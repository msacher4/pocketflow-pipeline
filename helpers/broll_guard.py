import logging

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
