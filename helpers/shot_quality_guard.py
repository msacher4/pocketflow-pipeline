import logging
import re

log = logging.getLogger("pocketflow-pipeline")

PHYSICS_QUESTION = {
    "type": "choice",
    "instructions": (
        "Could this shot actually be generated as a moving video, or is the "
        "motion it asks for implausible enough that the model will produce garbage?"
    ),
    "criteria": {
        "plausible": (
            "The motion is physically realizable as written: either the camera "
            "moves, or a visible agent drives the action (a hand turning a page, "
            "rain falling, light flickering, fabric moving), or an object moves "
            "under a stated force. A static scene with a slow camera move is fine."
        ),
        "implausible": (
            "The shot asks for motion nothing can account for: an object animates "
            "itself with no visible agent (a bow sliding across strings on its own, "
            "keys typing themselves, a page turning alone), an off-screen or "
            "invisible hand is implied to make the subject act, the motion "
            "contradicts the scene, or the description demands two mutually "
            "exclusive things at once. These prompts come out as smeared, "
            "morphing, or nonsensical footage."
        ),
    },
}

VO_MATCH_QUESTION = {
    "type": "choice",
    "instructions": (
        "Does the subject of this shot belong to the topic the voice-over talks "
        "about?"
    ),
    "criteria": {
        "related": (
            "The shot's subject comes from the topic named or implied by the "
            "voice-over: it shows it literally, it is a direct consequence of it, "
            "or it is the very object, place or action the voice-over is about. "
            "An abstract voice-over (an invitation, a choice, a warning, a feeling) "
            "may be illustrated by an atmosphere, a place or a detail of that SAME "
            "world — a neon corridor for 'choose your side', a stage for 'she sings "
            "to no one', a dark arena for 'the fight is rigged'. What matters is "
            "that the subject belongs to this voice-over's topic and not to another "
            "one."
        ),
        "unrelated": (
            "The shot's subject belongs to a different topic entirely, so that "
            "linking it to the voice-over would require inventing a story: an "
            "instrument lying on a desk for a voice-over about a voice that lands "
            "the final damage, a kitchen for a voice-over about a sword fight, a "
            "beach for a voice-over about a server outage. Sharing a mood, a "
            "colour or a tone is NOT enough."
        ),
    },
}

_PHYSICS_RE = re.compile(r"^(?:plausible|implausible)$")
_MATCH_RE = re.compile(r"^(?:related|unrelated)$")

PHYSICS_BLOCKING = "implausible"
VO_MATCH_BLOCKING = "unrelated"


async def classify_shot(prompt: str, voice_over: str = "",
                        character_name: str = "",
                        franchise: str = "") -> dict:
    """Pose physiques + lisibilité de la VO en UNE seule requête JEV.

    Renvoie {'ok': True, 'physics': {'choice', 'confidence'},
    'vo_match': {'choice', 'confidence'}} ou {'ok': False, 'reason': ...} —
    JAMAIS de levée d'exception : un JEV en panne doit laisser passer le script,
    jamais le tuer.

    Une seule requête pour les deux questions : ajouter une seconde question
    doublait la latence du validateur ALT pour rien (les deux questions portent
    sur le même prompt et la même VO).
    """
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
                "questions": {
                    "physics": PHYSICS_QUESTION,
                    "vo_match": VO_MATCH_QUESTION,
                },
            },
        )
        if resp.status_code != 200:
            log.warning(f"shot_quality_guard: JEV HTTP {resp.status_code} {resp.text[:200]}")
            return {"ok": False, "reason": f"HTTP {resp.status_code}"}
        answers = resp.json().get("answers", {}) or {}

        out: dict = {"ok": True}
        for key, pattern in (("physics", _PHYSICS_RE), ("vo_match", _MATCH_RE)):
            ans = answers.get(key) or {}
            choice, confidence = ans.get("choice"), ans.get("confidence")
            if not choice or not pattern.match(str(choice)):
                log.warning(f"shot_quality_guard: unexpected {key} answer {ans!r}")
                return {"ok": False, "reason": f"unexpected {key} answer"}
            out[key] = {"choice": str(choice),
                        "confidence": float(confidence or 0.0)}
        return out
    except Exception as e:
        log.warning(f"shot_quality_guard: JEV call failed: {e}")
        return {"ok": False, "reason": str(e)[:200]}
