import json
import logging
from pathlib import Path

log = logging.getLogger("pocketflow-pipeline")

USED_ARTICLES_PATH = Path(__file__).parent / "used_articles.json"


def load_used_articles() -> set:
    try:
        if USED_ARTICLES_PATH.is_file():
            data = json.loads(USED_ARTICLES_PATH.read_text())
            return set(data.get("used", []))
    except Exception as e:
        log.warning(f"used_articles load failed: {e}")
    return set()


def _save(used: set) -> None:
    # On garde un historique borné (400 dernières entrées) pour éviter la croissance infinie.
    trimmed = list(used)[-400:]
    try:
        USED_ARTICLES_PATH.write_text(json.dumps({"used": trimmed}))
    except Exception as e:
        log.warning(f"used_articles save failed: {e}")


def mark_used(url: str) -> None:
    used = load_used_articles()
    used.add(url)
    _save(used)


def is_used(url: str) -> bool:
    return url in load_used_articles()


def clear_used() -> int:
    """Vide la liste des articles déjà traités. Retourne le nb d'entrées vidées."""
    used = load_used_articles()
    n = len(used)
    if n:
        try:
            USED_ARTICLES_PATH.write_text(json.dumps({"used": []}))
        except Exception as e:
            log.warning(f"used_articles clear failed: {e}")
    return n
