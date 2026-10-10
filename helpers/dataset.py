"""Dataset d'entraînement automatique des VO validées (chemin alt).

Chaque script approuvé via `validate_sw_alt` est stocké en JSONL (un exemple
par ligne) dans `datasets/sw_alt.jsonl`. On conserve **tout le contexte**
(article, thinking_agent, topic, audio_mood) mais le champ `script` ne
contient **que les VO** (une par ligne, ordre du script) ; `vo_lines` duplique
la même liste pour l'entraînement direct.

L'origine de l'approbation est stockée dans `source` (bouton Approuver,
feedback, ✏️ edit, ⚡ boost).

Les runs en mode auto-approve (tests/debug) sont ignorés pour garder le
dataset propre.
"""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("pocketflow-pipeline")

DATASET_DIR = Path(__file__).parent.parent / "datasets"
DATASET_PATH = DATASET_DIR / "sw_alt.jsonl"

_VO_LINE_RE = re.compile(r"^\s*-?\s*VO\s*:\s*(.+?)\s*$", re.IGNORECASE)


def extract_vo_lines(script: str) -> list[str]:
    """Extrait les lignes VO d'un script (ordre du script, texte seul)."""
    out = []
    for line in (script or "").splitlines():
        m = _VO_LINE_RE.match(line)
        if m and m.group(1):
            out.append(m.group(1).strip())
    return out


def _article_context(shared: dict) -> dict:
    """Construit le contexte article depuis `selected_article` (schéma stable)."""
    a = shared.get("selected_article", {}) or {}
    return {
        "title": a.get("title", ""),
        "source": a.get("source", ""),
        "url": a.get("url", ""),
        "score": a.get("score", 0),
        "character_name": a.get("character_name", ""),
        "franchise": a.get("franchise", ""),
        "hook_angle": a.get("hook_angle", ""),
        "content": a.get("synthesis") or a.get("description") or a.get("summary") or "",
    }


def build_record(shared: dict, source: str = "approve") -> dict:
    """Construit l'enregistrement complet : contexte + `script` = VO seules."""
    script = shared.get("script", "") or ""
    vo_lines = extract_vo_lines(script)
    return {
        "id": str(shared.get("pipeline_id", "")),
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "topic": shared.get("topic", ""),
        "audio_mood": shared.get("audio_mood") or shared.get("_audio_mood_locked", ""),
        "article": _article_context(shared),
        "thinking_agent": shared.get("thinking_agent"),
        "script": "\n".join(vo_lines),
        "vo_lines": vo_lines,
    }


def record_validated_script(shared: dict, source: str = "approve") -> bool:
    """Append un exemple validé au dataset JSONL. Retourne True si écrit.

    Ignoré en mode auto-approve (runs de test/debug) et si aucune VO.
    """
    from helpers.state import is_auto_approve

    if is_auto_approve():
        log.info("[dataset] auto-approve actif, capture ignorée")
        return False

    pipeline_id = str(shared.get("pipeline_id", ""))
    if pipeline_id.startswith("debug-"):
        log.info(f"[dataset] run de debug ({pipeline_id}), capture ignorée")
        return False

    record = build_record(shared, source)
    if not record["vo_lines"]:
        log.info("[dataset] aucune VO, capture ignorée")
        return False

    try:
        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        with DATASET_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"[dataset] écriture échouée ({type(e).__name__}: {e})")
        return False

    log.info(
        f"[dataset] VO capturées (id={pipeline_id}, "
        f"source={source}, {len(record['vo_lines'])} VO)"
    )
    return True


def add_manual_example(script: str, context: dict | None = None,
                       source: str = "manual") -> dict | None:
    """Ajoute une VO générée à la main (hors pipeline) avec son contexte.

    `context` peut contenir tout ou partie de : pipeline_id, topic,
    audio_mood/_audio_mood_locked, selected_article, thinking_agent.
    Retourne l'enregistrement écrit, ou None si aucune VO / écriture échouée.
    Action explicite de l'utilisateur : pas de garde auto-approve ici.
    """
    from datetime import datetime, timezone

    vo_lines = extract_vo_lines(script)
    if not vo_lines:
        log.info("[dataset] ajout manuel ignoré : aucune VO")
        return None
    ctx = context or {}
    shared = {
        "pipeline_id": ctx.get("pipeline_id", f"manual-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"),
        "script": script,
        "topic": ctx.get("topic", ""),
        "audio_mood": ctx.get("audio_mood", ""),
        "_audio_mood_locked": ctx.get("_audio_mood_locked", ""),
        "selected_article": ctx.get("selected_article") or ctx.get("article") or {},
        "thinking_agent": ctx.get("thinking_agent"),
    }
    # `article` direct (déjà au bon schéma) prioritaire sur selected_article.
    record = build_record(shared, source)
    if isinstance(ctx.get("article"), dict) and ctx["article"].get("title"):
        record["article"] = ctx["article"]
    try:
        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        with DATASET_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"[dataset] ajout manuel échoué ({type(e).__name__}: {e})")
        return None
    log.info(f"[dataset] ajout manuel (id={record['id']}, {len(vo_lines)} VO)")
    return record
