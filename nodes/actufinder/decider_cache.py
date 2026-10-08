import json
import logging
from pathlib import Path

log = logging.getLogger("pocketflow-pipeline")

DECIDER_CACHE_PATH = Path(__file__).parent / "decider_cache.json"
# Cache borné : on garde les DECIDER_CACHE_CAP dernières entrées écrites.
DECIDER_CACHE_CAP = 2000


def load_decider_cache() -> dict:
    """Retourne {url: verdict}, verdict = dict avec 'title' et les champs ai_*."""
    try:
        if DECIDER_CACHE_PATH.is_file():
            data = json.loads(DECIDER_CACHE_PATH.read_text())
            cache = data.get("verdicts", {})
            if isinstance(cache, dict):
                return cache
    except Exception as e:
        log.warning(f"decider_cache load failed: {e}")
    return {}


def save_decider_cache(cache: dict) -> None:
    if len(cache) > DECIDER_CACHE_CAP:
        cache = dict(list(cache.items())[-DECIDER_CACHE_CAP:])
    try:
        DECIDER_CACHE_PATH.write_text(json.dumps({"verdicts": cache}))
    except Exception as e:
        log.warning(f"decider_cache save failed: {e}")


def cached_verdict_for(cache: dict, url: str, title: str):
    """Verdict en cache si l'URL est connue ET le titre inchangé (invalidation
    si le feed a remplacé l'article à la même URL)."""
    entry = cache.get(url)
    if not entry:
        return None
    if entry.get("title") != title:
        return None
    return entry