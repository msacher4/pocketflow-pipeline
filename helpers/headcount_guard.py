import asyncio
import json
import logging

import httpx

from config import OPENROUTER_API_KEY, OPENROUTER_DECISIONS_URL, OPENROUTER_MODEL

log = logging.getLogger("pocketflow-pipeline")

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"

HEADCOUNT_QUESTION = {
    "type": "choice",
    "instructions": (
        "How many distinct people are described as visible in the video prompt?"
    ),
    "criteria": {
        "no_person": (
            "NO visible person at all: an object, a place, an empty stage or "
            "room, a landscape, a lighting setup, a detail in close-up, or a "
            "crowd seen only from behind / from a distance / out of focus"
        ),
        "one_person": (
            "Exactly one visible person (she/her, alone, solo, portrait of a single "
            "character reacting to their surroundings)"
        ),
        "two_people": (
            "Two distinct visible people who interact with each other"
        ),
        "crowd": (
            "A crowd of unnamed background people (spectators, audience, crowd, "
            "bystanders, many people, extras)"
        ),
    },
}

HEADCOUNT_CHOICES = ("no_person", "one_person", "two_people", "crowd")

_HTTP = None


def _client() -> httpx.AsyncClient:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.AsyncClient(timeout=10.0, headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        })
    return _HTTP


async def classify_headcount(prompt: str) -> dict:
    """Demande à JEV combien de personnes le prompt décrit.
    Renvoie {'ok': True, 'choice': 'no_person'|'one_person'|'two_people'|'crowd',
    'confidence': 0-1} ou {'ok': False, 'reason': ...} — JAMAIS de levée
    d'exception (le run continue).

    `no_person` existe pour les plans T2V/b-roll : sans elle, un insert de scène
    vide était forcé dans `one_person` et le validateur ALT exigeait alors
    'alone' dessus — la règle b-roll et le verrou headcount se marchaient dessus."""
    if not OPENROUTER_API_KEY:
        return {"ok": False, "reason": "no OPENROUTER_API_KEY"}
    try:
        resp = await _client().post(
            OPENROUTER_DECISIONS_URL,
            json={
                "model": OPENROUTER_MODEL,
                "state": {"prompt": prompt},
                "questions": {"headcount": HEADCOUNT_QUESTION},
            },
        )
        if resp.status_code != 200:
            log.warning(f"headcount_guard: JEV HTTP {resp.status_code}: {resp.text[:200]}")
            return {"ok": False, "reason": f"HTTP {resp.status_code}"}
        body = resp.json()
        ans = body.get("answers", {}).get("headcount") or {}
        choice = ans.get("choice")
        confidence = ans.get("confidence")
        if choice not in HEADCOUNT_CHOICES:
            log.warning(f"headcount_guard: unexpected answer {ans!r}")
            return {"ok": False, "reason": "unexpected answer"}
        return {
            "ok": True,
            "choice": choice,
            "confidence": float(confidence or 0.0),
        }
    except Exception as e:
        log.warning(f"headcount_guard: JEV call failed: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def apply_headcount_guard(prompt: str, negative: str | None, verdict: dict) -> tuple[str, str | None, dict]:
    """Applique le verrou headcount sur le prompt et le négatif combiné.
    Retourne (prompt, negative, meta). Meta décrit l'action faite (log/trace)."""
    anti_people = "second person, two people, extra people, crowd"
    if not verdict.get("ok"):
        # Erreur / pas de clé : fallback conservateur (choix 1 validé) — anti-gens
        # seulement, jamais 'alone' quand on doute.
        neg = f"{negative}, {anti_people}" if negative else anti_people
        return prompt, neg, {"mode": "fallback", "reason": verdict.get("reason", "unknown")}
    choice = verdict["choice"]
    conf = verdict["confidence"]
    if choice == "one_person" and conf >= 0.8:
        meta = {"mode": "one_person", "confidence": conf}
        if "alone" not in prompt.lower():
            cleaned = prompt.rstrip()
            if cleaned.endswith("."):
                cleaned = cleaned[:-1]
            prompt = f"{cleaned}, alone."
            meta["injected_alone"] = True
        else:
            meta["alone_already"] = True
        neg = f"{negative}, {anti_people}" if negative else anti_people
        if negative and "second person" in negative:
            neg = negative
            meta["anti_people_already"] = True
        return prompt, neg, meta
    # one_person basse confiance (< 0.8) : on doute -> fallback conservateur,
    # anti-gens seulement, jamais 'alone' (choix 1 validé).
    if choice == "one_person":
        neg = f"{negative}, {anti_people}" if negative else anti_people
        return prompt, neg, {"mode": "fallback", "confidence": conf, "reason": "low confidence"}
    # two_people / crowd / no_person : on ne modifie rien. no_person en
    # particulier ne doit JAMAIS recevoir 'alone' ni d'anti-people : c'est le
    # cas normal d'un insert b-roll (décor vide, objet, mains en détail).
    return prompt, negative, {"mode": choice if choice else "conservative", "confidence": conf}


def _sfx_choice_question(catalog: list[dict]) -> dict:
    """Question JEV `choice` pour choisir UN effet sonore du catalogue VFX (ou
    "no_sfx"). Deux options antinomiques + descripteurs par fichier : JEV répond
    le meilleur match pour l'intention de la transition décrite dans le state."""
    criteria = {"no_sfx": "Aucun effet sonore n'est utile sur cette transition."}
    for e in catalog:
        file = e.get("file", "")
        if not file:
            continue
        cat = e.get("category", "")
        dur = e.get("duration_s")
        intent = {
            "whooshes": "transition de plan, changement de rythme (balayage)",
            "impacts": "impact métallique fort, coupe franche",
            "risers": "montée de tension juste avant un cut",
            "pops": "surbrillance d'un mot-clé au moment du cut",
        }.get(cat, cat)
        d = f" ({dur}s)" if dur else ""
        criteria[file] = f"{intent}{d} — fichier {file}"
    return {
        "type": "choice",
        "instructions": (
            "Choisis l'effet sonore qui amplifie le mieux CETTE transition vidéo "
            "(contexte fourni). Prends en compte la section, le rythme et le contenu "
            "des plans autour de la transition. NO_SFX si rien ne sert le moment."
        ),
        "criteria": criteria,
    }


async def choose_sfx_across_transitions(anchors: list[dict], catalog: list[dict]) -> list[int]:
    """Choisit OPTIONNELLEMENT un SFX par transition via JEV.

    - Un appel JEV par transition (parallèle) ; state = contexte de la transition.
    - `no_sfx` ou basse confiance ou erreur JEV → aucune entrée pour cette ancre
      (les SFX sont optionnels par design ; jamais de crash).
    - Retourne la liste des `anchor_index` retenus (index de transition valides) ;
      la résolution fichier/catégorie reste déterministe (FIXE : on utilise le
      premier candidat de la catégorie dominante de l'index).

    NOTE : cette fonction choisit uniquement DANS QUELLES transitions mettre un
    SFX (décision sémantique = le job de JEV). Le fichier lui-même est réattribué
    par `_select_sfx` du pipeline à partir du catalogue (validation en code)."""
    if not anchors or not catalog:
        return []
    question = _sfx_choice_question(catalog)

    async def _one(anchor: dict) -> int | None:
        try:
            resp = await _client().post(
                OPENROUTER_DECISIONS_URL,
                json={
                    "model": MODEL,
                    "state": {
                        "transition": {
                            "anchor_index": anchor.get("anchor_index"),
                            "section": anchor.get("section", ""),
                            "next_plan_content": (anchor.get("content") or "")[:300],
                        }
                    },
                    "questions": {"sfx_pick": question},
                },
            )
            if resp.status_code != 200:
                log.warning(f"sfx_pick: JEV HTTP {resp.status_code}")
                return None
            ans = resp.json().get("answers", {}).get("sfx_pick") or {}
            choice = ans.get("choice")
            confidence = float(ans.get("confidence") or 0.0)
            if choice and choice != "no_sfx" and confidence >= 0.55:
                return anchor.get("anchor_index")
            return None
        except Exception as e:
            log.warning(f"sfx_pick: JEV call failed: {e}")
            return None

    picked = await asyncio.gather(*[_one(a) for a in anchors])
    return [idx for idx in picked if idx is not None]