"""VoShortener — raccourcit les VO trop longues par RÉÉCRITURE, alternative au
reformat complet. Quand la validation pydantic rejette un script pour VO en
sur-budget, ce node réécrit UNIQUEMENT les lignes VO fautives en plus court
(même sens, moins de mots) au lieu de régénérer tout le script comme AltSG.

Le LLM renvoie uniquement les VO corrigées indexées par plan (`edits`), jamais
le script complet → aucune dérive de structure, moins de tokens. Un filet
déterministe (en dernier recours) retire des phrases COMPLÈTES en fin de VO
avec LE MÊME comptage que le validateur (contractions `she's` = 2 mots) :
jamais de coupe au milieu d'une phrase ni d'un mot.

Ne touche jamais à Video:/titres Plan/sections/horaires/Audio:."""

import json
import logging
import re
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, LLMJSONQuoteError
from .script_timing import parse_plans, plan_budget_words, plan_vo_words

log = logging.getLogger("pocketflow-pipeline")

MAX_WORDS = 20
_VO_RE = re.compile(r"^(\s*[-*]?\s*VO\s*:\s*)(.+)$", re.IGNORECASE)
_SENT_RE = re.compile(r"[^.!?]+[.!?]+")


def _protected_names(shared) -> tuple[str, str]:
    """Nom du personnage + franchise à ne JAMAIS supprimer en raccourcissant
    (sinon le validateur pydantic rejette le script pour 'pas nommé' et on
    boucle avec AltSG — cf. run du 21/09 : 'Genshin Impact' jeté par le
    shortener → 6 régénérations)."""
    try:
        character = (shared.get("selected_article") or {}).get("character") or {}
    except AttributeError:
        character = {}
    name = (character.get("name") or "").strip()
    franchise = (character.get("franchise") or "").strip()
    return name, franchise


def _count_words(text: str) -> int:
    """Comptage OFFICIEL du pipeline : les contractions comptent pour 2 mots
    (séparation sur l'apostrophe). Identique à plan_vo_words / pydantic."""
    return len([w for w in text.replace("'", " ").split() if w.strip()])


def _drop_last_sentence(text: str) -> str:
    """Retire la DERNIÈRE phrase complète de la VO (phrase grammaticalement
    entière, jamais coupée). Retourne le texte inchangé si une seule phrase."""
    parts = [m.group(0).strip() for m in _SENT_RE.finditer(text)]
    if len(parts) <= 1:
        return text
    return " ".join(parts[:-1]).strip()


def _drop_last_clause(text: str) -> str:
    """Retire la dernière PROPOSITION séparée par une virgule (ou après le
    dernier 'but'/'and'/'so'...) : remonte un cran plus fin que la phrase, mais
    ne coupe jamais au milieu d'un mot ni d'une proposition. Inchangé sinon."""
    commas = list(re.finditer(r",\s*", text))
    if not commas:
        return text
    cut = commas[-1].start()
    keep = text[:cut].rstrip()
    if not keep.strip():
        return text
    return keep


def _shorten_words(text: str, budget: int) -> str:
    """Dernier recours : garde les PREMIERS mots de la VO jusqu'à tenir dans le
    budget (frontières de mots uniquement, comptage officiel incluant les
    contractions). Ne coupe jamais un mot."""
    tokens = text.split()
    kept = []
    for w in tokens:
        candidate = " ".join(kept + [w])
        if _count_words(candidate) > budget:
            break
        kept.append(w)
    if not kept:
        return text
    return " ".join(kept).rstrip() if len(kept) < len(tokens) else text


def _reduce_vo(text: str, budget: int) -> str:
    """Réduit une VO au budget sans jamais couper un mot. Évalue chaque palier
    (phrase, proposition, premiers mots) sur le texte ORIGINAL et retient le
    résultat le plus long qui tienne dans le budget."""
    if _count_words(text) <= budget:
        return text
    candidates = []
    for fn in (_drop_last_sentence, _drop_last_clause, _shorten_words):
        nxt = fn(text, budget) if fn is _shorten_words else fn(text)
        if nxt != text and _count_words(nxt) <= budget:
            candidates.append(nxt)
    if not candidates:
        return text
    # le candidat le plus long (le plus proche du budget)
    candidates.sort(key=_count_words, reverse=True)
    return candidates[0]


class VoShortenerNode(AsyncNode):
    """Réécrit UNIQUEMENT les VO dépassant la capacité de leur plan, en gardant
    la structure du script strictement intacte."""

    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "vo_shortener"
        await _set_state(**_shared_snapshot(shared))
        return shared

    def _over_budget_plans(self, script: str) -> list[dict]:
        """Plans dont la VO dépasse la capacité : somme des VO du plan >
        budget (même règle que pacing_errors/pydantic) OU une ligne VO isolée
        > MAX_WORDS."""
        out = []
        for p in parse_plans(script):
            budget = plan_budget_words(p)
            total = plan_vo_words(p)
            over_line = any(_count_words(v["text"]) > MAX_WORDS for v in p["vo_lines"])
            if (total > budget or over_line) and p["vo_lines"]:
                out.append({
                    "plan": p["num"],
                    "budget": budget,
                    "vo_lines": p["vo_lines"],
                })
        return out

    async def exec_async(self, shared):
        soul = load_soul("vo_shortener")
        script = shared.get("script", "")
        self._input_script = script
        name, franchise = _protected_names(shared)
        names_hint = ""
        if name or franchise:
            keep = " et ".join(f"'{n}'" for n in (name, franchise) if n)
            names_hint = (
                f"\nNOMS À PRÉSERVER COÛTE QUE COÛTE : {keep}. Ces noms sont "
                f"EXIGÉS dans les VO par la validation (script rejeté s'ils "
                f"manquent) : ne les supprime JAMAIS en raccourcissant, coupe "
                f"ailleurs dans la phrase.\n"
            )
        ctx = (
            f"\n--- SCRIPT À CORRIGER ---\n{script}\n--- FIN SCRIPT ---\n\n"
            f"{names_hint}"
            f"La validation a rejeté le script pour VO trop longue. Identifie les "
            f"plans dont la VO dépasse la capacité de leur plan (I2V≈3s → ≈5-6 "
            f"mots, T2V≈4s → ≈7-8 mots, I2V+T2V=7s → ≈18 mots, 2 T2V=8s → ≈20 "
            f"mots ; les contractions comptent DOUBLE ; max 2 vidéos/plan) et "
            f"RÉÉCRIS ces VO en plus court — jamais de coupe. Renvoie UNIQUEMENT "
            f"les VO corrigées en JSON : {{\"shortened\": N, \"edits\": "
            f"{{\"7\": [\"nouvelle VO\"], \"3\": [\"nouvelle VO\"]}}}}"
        )

        llm_resp = await call_llm(
            LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx,
            max_tokens=8192, timeout=600, temperature=0.2,
        )
        _trace_llm(shared, "vo_shortener", "exec", LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, llm_resp)
        try:
            decision = _extract_json(llm_resp)
        except LLMJSONQuoteError as e:
            # Le LLM a mis des guillemets doubles dans les VO : 1 appel correctif
            # ciblé (même pattern que ThinkingAgent/AssetPlanner) au lieu de tuer
            # le run (cf. run du 22/09 : shortener mort sur des guillemets).
            log.warning(f"VoShortener EXEC -> JSON quote error, requesting clean re-emit: {e}")
            retry_ctx = (
                f"{ctx}\n\n--- RÉPONSE PRÉCÉDENTE INVALIDE ---\nTon JSON est invalide : "
                f"tu as écrit des guillemets doubles \" à l'INTÉRIEUR des VO. Réémet "
                f"EXACTEMENT les mêmes edits, sans rien changer au contenu, mais en "
                f"remplaçant TOUS les guillemets doubles internes par des apostrophes "
                f"simples '. Retourne UNIQUEMENT le JSON corrigé "
                f"({{\"shortened\": N, \"edits\": {{\"7\": [\"nouvelle VO\"]}}}})."
            )
            try:
                retry_resp = await call_llm(
                    LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx,
                    max_tokens=8192, timeout=600, temperature=0.2,
                )
                _trace_llm(shared, "vo_shortener", "exec_retry_quotes", LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx, retry_resp)
                decision = _extract_json(retry_resp)
                log.info("VoShortener EXEC -> clean re-emit parsed OK")
            except Exception as e2:
                # Dernier recours : sans les edits LLM, le filet déterministe
                # (phrases complètes, jamais de mot coupé) raccourcit seul.
                log.warning(f"VoShortener EXEC -> re-emit still invalid ({e2}), filet déterministe seul")
                decision = None

        edits = decision.get("edits") if isinstance(decision, dict) else None
        fixed_script = self._apply_edits(script, edits) if edits else script
        over = self._over_budget_plans(fixed_script)
        if over:
            log.warning(
                f"VoShortener EXEC -> LLM insuffisant, filet déterministe (phrases "
                f"complètes) sur plans {sorted(o['plan'] for o in over)}")
            fixed_script = self._apply_deterministic(fixed_script, over)
            over = self._over_budget_plans(fixed_script)
            if over:
                raise RuntimeError(
                    f"VoShortener: VO encore trop longues après filet: "
                    f"plans {sorted({o['plan'] for o in over})}")

        n_shortened = None
        if isinstance(decision, dict):
            n_shortened = decision.get("shortened")
        log.info(f"VoShortener EXEC -> script length={len(fixed_script)}, "
                 f"shortened={n_shortened if n_shortened is not None else '?'}")
        return json.dumps({"script": fixed_script}, ensure_ascii=False)

    def _apply_edits(self, script: str, edits) -> str:
        """Remplace les lignes VO des plans corrigés par les nouvelles VO fournies
        par le LLM. Un plan à plusieurs VO imprévues est ramené aux nouvelles
        fournies (souvent 1) : corrige au passage le défaut '2 VO dans un plan'."""
        if not isinstance(edits, dict) or not edits:
            return script

        lines = (script or "").splitlines()
        plans = parse_plans(script)

        per_plan: dict[int, list[str]] = {}
        for pn, texts in edits.items():
            try:
                pn = int(pn)
            except (TypeError, ValueError):
                continue
            if not isinstance(texts, list):
                texts = [texts]
            kept = [str(t).strip() for t in texts if str(t).strip()]
            if kept:
                per_plan[pn] = kept
        if not per_plan:
            return script

        vo_plan_of = {v["line"]: p["num"] for p in plans for v in p["vo_lines"]}
        anchor: dict[int, tuple[int, str]] = {}
        for p in plans:
            if p["num"] in per_plan and p["vo_lines"]:
                first = p["vo_lines"][0]["line"]
                m = _VO_RE.match(lines[first])
                anchor[p["num"]] = (first, m.group(1) if m else "VO: ")

        remove = {ln for ln, pn in vo_plan_of.items() if pn in per_plan}
        out: list[str] = []
        done: set[int] = set()
        for i, raw in enumerate(lines):
            if i in remove:
                pn = vo_plan_of[i]
                if pn not in done and pn in anchor:
                    first, prefix = anchor[pn]
                    for t in per_plan[pn]:
                        out.append(prefix + t)
                    done.add(pn)
                continue
            out.append(raw)
        return "\n".join(out)

    def _apply_deterministic(self, script: str, over) -> str:
        """Filet sans LLM : réduit chaque VO en sur-budget par paliers (phrase →
        proposition → premiers mots), en comptant avec le comptage officiel.
        Ne coupe JAMAIS un mot."""
        lines = (script or "").splitlines()
        repl: dict[int, str] = {}

        for o in over:
            budget = o["budget"]
            vos = sorted(o["vo_lines"], key=lambda v: v["line"])
            texts = {v["line"]: v["text"] for v in vos}
            total = sum(_count_words(t) for t in texts.values())

            if total > budget:
                # réduit les VO par ordre inverse (dernière d'abord) jusqu'à
                # tenir dans le budget, en retirant une phrase puis une proposition
                for v in reversed(vos):
                    if total <= budget:
                        break
                    ln = v["line"]
                    share = budget - (total - _count_words(texts[ln]))
                    reduced = _reduce_vo(texts[ln], max(share, 0))
                    if reduced != texts[ln]:
                        total -= _count_words(texts[ln]) - _count_words(reduced)
                        texts[ln] = reduced

            for v in vos:
                ln = v["line"]
                cap = min(MAX_WORDS, budget)
                if _count_words(texts[ln]) > cap:
                    texts[ln] = _reduce_vo(texts[ln], cap)

            for v in vos:
                ln = v["line"]
                m = _VO_RE.match(lines[ln])
                if m and texts[ln] != v["text"]:
                    repl[ln] = m.group(1) + texts[ln]

        if not repl:
            return script
        return "\n".join(repl.get(i, l) for i, l in enumerate(lines))

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("VoShortener POST -> exec is not valid JSON")
            shared["_current_step"] = "vo_shortener_error"
            shared["_error"] = "VoShortener: exec is not valid JSON"
            shared["steps"].append({
                "step": "vo_shortener", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VoShortener aborted: exec is not valid JSON")

        raw_script = decision.get("script", "")
        if not raw_script.strip():
            log.warning("VoShortener POST -> script is empty")
            shared["_current_step"] = "vo_shortener_error"
            shared["_error"] = "VoShortener: empty script"
            shared["steps"].append({
                "step": "vo_shortener", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VoShortener aborted: empty script")

        shared["script"] = raw_script
        shared["_reformat_error"] = ""
        shared["_current_step"] = "vo_shortener_done"
        shared["steps"].append({
            "step": "vo_shortener", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": self._input_script[:200],
            "output": raw_script[:200],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"