import logging
import re

log = logging.getLogger("pocketflow-pipeline")

BROLL_QUESTION = {
    "type": "choice",
    "instructions": (
        "Does this shot show the named character as a visible person?"
    ),
    "criteria": {
        "character_visible": (
            "The named character (or a recognisable stand-in for her) appears as "
            "a visible person: her face, her body, a clear silhouette of her, or "
            "she is explicitly named in the shot description. This also covers "
            "an eye or any other facial feature in close-up (iris, eyelid, "
            "brow, jaw), and her face showing only as a reflection on a surface "
            "(screen, glass, eye, liquid, poster)"
        ),
        "b_roll": (
            "The named character does NOT appear as a person. The shot is an "
            "insert that carries the idea behind the voice-over: a place, an "
            "object, hands or gear in close-up detail, an empty stage, lights, a "
            "landscape, or a crowd seen only from behind or at a distance. "
            "Hands and gear are acceptable, but never a facial feature"
        ),
    },
}

# ---------------------------------------------------------------------------
# Filet déterministe (zéro LLM)
#
# `BROLL_QUESTION` ci-dessus demande à JEV si une ligne T2V est un vrai
# b-roll. Le filtre ci-dessous fait le même tri sur le TEXTE, sans appel
# réseau : c'est ce dont `repair_pacing_script` a besoin, car ce filet
# s'exécute quand le LLM a déjà produit un script et qu'on ne veut pas
# repayer un appel modèle pour ajouter une 2e vidéo.
#
# Les deux doivent rester alignés : `FACIAL_FEATURE_RE` reprend à l'identique
# les termes cités par `criteria["character_visible"]` et refusés par
# `criteria["b_roll"]` (œil, iris, paupière, sourcil, machoire, reflet du
# visage). `tests/test_i2v_broll.py` verrouille la parité.
# ---------------------------------------------------------------------------

# Visages découvrables sans LLM : parties de visage en détail, expressions et
# le mot « face » lui-même. Ne contient PAS « reflection » en clair : un reflet
# d'objet (néon sur un liquide) est un vrai b-roll, c'est le REFLET DU VISAGE
# qui est interdit — et il est déjà couvert ici (« her face » tombe aussi sous
# le filtre personne, cf. `character_present`).
FACIAL_FEATURE_RE = re.compile(
    r"\b(?:"
    # `face` exclut `face-up` / `face down` : ce sont des ORIENTATIONS
    # d'objet (« a smartphone lying face-up »), pas un visage. Les laisser
    # tomber supprimait le sujet du plan CTA tout entier.
    r"face(?![- ]?(?:up|down))|eye|eyes|iris|eyelid|eyelids|eyelash|eyelashes|"
    r"brow|brows|eyebrow|eyebrows|jaw|cheek|cheeks|lip|lips|"
    r"mouth|nose|forehead|gaze|smile|smiling|grin|frown|"
    r"expression|glare|stare|staring"
    r")\b",
    re.IGNORECASE,
)


def is_facial(video_text: str) -> bool:
    """True si la ligne traite d'une part de visage ou d'une expression.

    Contrat partagé avec le critère JEV : mains et objets acceptés, jamais une
    feature faciale."""
    return bool(FACIAL_FEATURE_RE.search(video_text or ""))


def is_true_broll(video_text: str) -> bool:
    """True si la ligne est un VRAI b-roll : ni personnage, ni détail de visage.

    Version déterministe du critère `b_roll` de `BROLL_QUESTION`. Utilisée par
    `to_broll_variant` (script_timing) pour garantir que le filet de
    réparation ne produit jamais un T2V interdit, et par les tests pour
    verrouiller la frontière main-autorisée / visage-interdit.
    """
    # Import local : script_timing importe ce module au niveau module, un
    # import réciproque ici au niveau module formerait un cycle.
    from nodes.scriptwriter.script_timing import character_present

    text = video_text or ""
    if is_facial(text):
        return False
    return not character_present(text)


async def classify_broll(prompt: str, voice_over: str = "",
                         character_name: str = "",
                         franchise: str = "") -> dict:
    """Demande à JEV si un plan T2V montre le personnage central ou porte
    l'idée de sa VO (b-roll).

    Renvoie {'ok': True, 'choice': 'character_visible'|'b_roll',
    'confidence': 0-1} ou {'ok': False, 'reason': ...} — JAMAIS de levée
    d'exception : un JEV en panne doit laisser passer le script, jamais le tuer.
    """
    # Import tardif : les helpers config ne doivent pas être chargés à
    # l'import du module pydantic_validation (qui est importé par les tests).
    from helpers.headcount_guard import _client
    from config import OPENROUTER_API_KEY, OPENROUTER_DECISIONS_URL, OPENROUTER_MODEL

    if not OPENROUTER_API_KEY:
        return {"ok": False, "reason": "no OPENROUTER_API_KEY"}
    try:
        resp = await _client().post(
            OPENROUTER_DECISIONS_URL,
            json={
                "model": OPENROUTER_MODEL,
                "state": {
                    "shot_prompt": prompt,
                    "voice_over": voice_over,
                    "character_name": character_name,
                    "franchise": franchise,
                },
                "questions": {"b_roll": BROLL_QUESTION},
            },
        )
        if resp.status_code != 200:
            log.warning(f"broll_guard: JEV HTTP {resp.status_code}: {resp.text[:200]}")
            return {"ok": False, "reason": f"HTTP {resp.status_code}"}
        ans = resp.json().get("answers", {}).get("b_roll") or {}
        choice = ans.get("choice")
        confidence = ans.get("confidence")
        if choice not in ("character_visible", "b_roll"):
            log.warning(f"broll_guard: unexpected answer {ans!r}")
            return {"ok": False, "reason": "unexpected answer"}
        return {"ok": True, "choice": choice, "confidence": float(confidence or 0.0)}
    except Exception as e:
        log.warning(f"broll_guard: JEV call failed: {e}")
        return {"ok": False, "reason": str(e)[:200]}
