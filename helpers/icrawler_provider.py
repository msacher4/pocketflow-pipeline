import logging
import os
import re

from icrawler.downloader import ImageDownloader
from icrawler.parser import Parser

log = logging.getLogger("pocketflow-pipeline")

# Google n'a pas d'API officielle : icrawler scrape les pages de résultats. Google
# est plus exhaustif sur un personnage nommé mais se bloque plus vite, donc Bing
# sert de repli. Les deux crawlers acceptent min_size/max_size.
GOOGLE_FILTERS = {"type": "photo", "size": "large", "color": "color"}
# Bing : AUCUN filtre. Son endpoint /images/async ne renvoie rien quand on lui
# passe `size=large` (vérifié : 0 résultat avec filtre, 6 sans). La taille est
# déjà garantie en aval par `_probe` (MIN_SIZE).
BING_FILTERS = None
MIN_SIZE = (400, 400)


class BingImageParser(Parser):
    """Extracteur d'URL d'images Bing.

    Le parseur livré avec icrawler est cassé : `parse()` se termine sans
    `return` et renvoie donc `None`, ce que le worker de icrawler ne gère pas
    (`for task in self.parse(...)` → TypeError). Bing, lui, marche : les URLs
    sont dans les attributs `murl` des balises `<a class="iusc">`.

    On renvoie TOUJOURS une liste — vide si rien n'est trouvé — parce qu'un
    `None` tuerait le thread du parser.
    """

    _URL_RE = re.compile(r'murl&quot;:&quot;(.*?)&quot;')

    def parse(self, response):
        try:
            html = response.content.decode("utf-8", "ignore")
        except Exception:
            return []
        out, seen = [], set()
        for raw in self._URL_RE.findall(html):
            url = raw.replace("\\/", "/").replace("&amp;", "&")
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            out.append(url)
        return [{"file_url": url} for url in out]


class GoogleImageParser(Parser):
    """Extracteur d'URL d'images Google — plus d'URL à extraire.

    Google ne sert plus aucune image dans le HTML de `tbm=isch` (0 `.jpg`, 0
    `AF_initDataCallback` sur une requête sonde) : le rendu est integralement
    côté JS. Le parseur d'icrawler tombe donc dans son `return` implicite et
    renvoie `None`, ce qui casse le worker.

    On tente malgré tout les motifs historiques, mais on renvoie `[]` en dernier
    recours au lieu de `None`. Google reste en tête de chaîne comme demandé :
    s'il redevient exploitable, il gagne tout seul ; en attendant c'est Bing qui
    alimente la recherche.
    """

    _SCRIPT_RE = re.compile(rb"http[^\s\"'<>\\]*?\.(?:jpg|jpeg|png|webp|bmp)", re.I)

    def parse(self, response):
        urls, seen = [], set()
        try:
            for match in self._SCRIPT_RE.findall(response.content or b""):
                url = match.decode("utf-8", "ignore").replace("\\/", "/")
                url = url.split("&")[0].split('"')[0]
                if url.startswith("http") and url not in seen:
                    seen.add(url)
                    urls.append(url)
        except Exception:
            return []
        return [{"file_url": url} for url in urls]


class _RecordingDownloader(ImageDownloader):
    """Downloader icrawler qui retient l'URL source de chaque image écrite.

    Sans ça on se retrouve avec des fichiers .img sans provenance — exactement le
    problème qui a rendu le run Danbooru 20261004_184955 impossible à diagnostiquer.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: list[dict] = []

    def download(self, task, *args, **kwargs):
        super().download(task, *args, **kwargs)
        if task.get("success") and task.get("filename"):
            # `task["filename"]` est un nom RELATIF au root_dir du storage : le
            # reconstruire, sinon `_probe` ne trouve pas le fichier et tous les
            # candidats sont rejetés.
            with self.lock:
                self.records.append({
                    "url": task.get("file_url", ""),
                    "path": os.path.join(self.storage.root_dir, task["filename"]),
                })


def build_query(character: dict) -> str:
    """Requête de recherche = nom du personnage + oeuvre.

    Le nom seul est ambigu (cf. les tags homonymes Danbooru lilly_* qui ont renvoyé
    deux personnages différents) ; l'oeuvre lève l'ambiguïté.
    """
    name = (character.get("name") or "").strip()
    franchise = (character.get("franchise") or "").strip()
    return f"{name} {franchise}".strip() or name


def _run_crawl(engine_cls, parser_cls, query: str, out_dir: str, max_num: int,
               filters: dict | None) -> list[dict]:
    os.makedirs(out_dir, exist_ok=True)
    crawler = engine_cls(
        feeder_threads=1,
        parser_threads=1,
        downloader_threads=4,
        parser_cls=parser_cls,
        downloader_cls=_RecordingDownloader,
        storage={"root_dir": out_dir},
        log_level=logging.WARNING,
    )
    crawler.crawl(keyword=query, filters=filters, max_num=max_num, min_size=MIN_SIZE)
    return crawler.downloader.records


def _probe(path: str) -> dict | None:
    """Ouvre l'image et renvoie ses dimensions, ou None si fichier corrompu."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        return None
    if w < MIN_SIZE[0] or h < MIN_SIZE[1]:
        return None
    return {"w": w, "h": h}


def search_character_images(
    query: str,
    out_dir: str,
    max_num: int = 24,
    keep: int = 12,
) -> list[dict]:
    """Cherche des images de personnage via Google puis, si besoin, Bing.

    Google est gardé en tête comme demandé, mais il ne rend plus aucune image en
    HTML (rendu 100% JS) : en pratique c'est Bing qui alimente la recherche. Les
    deux parseurs renvoient toujours une liste, jamais None.

    Renvoie une liste de dicts {path, url, source, w, h} triée : portrait et grande
    surface d'abord (fond de vidéo vertical), le reste ensuite. Liste vide =
    personnage introuvable sur les deux moteurs.
    """
    from icrawler.builtin import BingImageCrawler, GoogleImageCrawler

    engines = (
        ("google", GoogleImageCrawler, GoogleImageParser, GOOGLE_FILTERS),
        ("bing", BingImageCrawler, BingImageParser, BING_FILTERS),
    )
    records: list[dict] = []
    seen_urls: set[str] = set()
    for source, engine_cls, parser_cls, filters in engines:
        try:
            fetched = _run_crawl(engine_cls, parser_cls, query, out_dir, max_num, filters)
        except Exception as e:
            log.warning(f"icrawler {source} échoué pour « {query} »: {e}")
            continue
        fresh = 0
        for rec in fetched:
            url = rec.get("url") or ""
            if not url or url in seen_urls:
                continue
            dims = _probe(rec["path"])
            if not dims:
                continue
            seen_urls.add(url)
            records.append({"path": rec["path"], "url": url, "source": source, **dims})
            fresh += 1
        log.info(f"icrawler {source}: {fresh} image(s) retenue(s) pour « {query} »")
        if len(records) >= keep:
            break
        # Google a rendu moins que ce qu'on veut -> on tente Bing.
    ranked = sorted(
        records,
        key=lambda r: (0 if r["h"] > r["w"] else 1, -(r["w"] * r["h"])),
    )
    return ranked[:keep]