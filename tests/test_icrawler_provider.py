"""Tests du fournisseur d'images icrawler (helpers/icrawler_provider.py).

Remplace Danbooru, qui mélangeait des personnages homonymes : `lilly_*` sur
Danbooru ramenait `lilly_(reverseblue)` et `lilly_candy`, deux personnages sans
rapport avec celui recherché. Ici la requête porte le nom ET l'oeuvre, et chaque
image conservée garde son URL source — sans quoi on reproduit le problème
opposé : des fichiers `.img` anonymes impossibles à diagnostiquer (run
20261004_184955).

Aucun réseau : `_run_crawl` est remplacé par un bouchon, on teste le tri, le
repli Google -> Bing, la déduplication et la tolérance aux pannes.
"""

import os
import sys
import tempfile
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from helpers import icrawler_provider as provider

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok  {label}")
    else:
        failures.append(label)
        print(f"  KO  {label}")


def _make_image(path: Path, w: int, h: int) -> Path:
    from PIL import Image
    Image.new("RGB", (w, h), (10, 20, 30)).save(path)
    return path


def main() -> int:
    print("[1] construction de la requête (nom + oeuvre)")
    check(provider.build_query({"name": "Lilly", "franchise": "Magical Sisters Lulutto"})
          == "Lilly Magical Sisters Lulutto",
          "nom + oeuvre concaténés")
    check(provider.build_query({"name": "Lilly", "franchise": ""}) == "Lilly",
          "oeuvre absente -> nom seul")
    check(provider.build_query({"name": "  Lilly  ", "franchise": "  X  "}) == "Lilly X",
          "espaces normalisés")
    check(provider.build_query({}) == "", "personnage vide -> requête vide")
    print("[1] construction de la requête : OK")

    print("\n[2] sonde d'image (_probe)")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        good = _make_image(tmp / "good.jpg", 800, 1200)
        dims = provider._probe(str(good))
        check(dims == {"w": 800, "h": 1200}, "image valide -> dimensions relues")

        small = _make_image(tmp / "small.jpg", 50, 60)
        check(provider._probe(str(small)) is None,
              f"image sous MIN_SIZE {provider.MIN_SIZE} -> rejetée")

        corrupt = tmp / "corrupt.jpg"
        corrupt.write_bytes(b"ceci n'est pas une image")
        check(provider._probe(str(corrupt)) is None, "fichier corrompu -> None, pas d'exception")

        check(provider._probe(str(tmp / "absent.jpg")) is None, "fichier absent -> None")
    print("[2] sonde d'image : OK")

    print("\n[3] tri des candidats (portrait et grande surface d'abord)")
    with tempfile.Temporarydir if False else tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cand_dir = tmp / "cand"
        cand_dir.mkdir()
        wide_big = _make_image(cand_dir / "wide.jpg", 3000, 1000)
        tall_small = _make_image(cand_dir / "tall.jpg", 500, 700)
        tall_big = _make_image(cand_dir / "tallbig.jpg", 1200, 2400)

        def fake_run(engine_cls, parser_cls, query, out_dir, max_num, filters):
            # google d'abord, bing ensuite ; large/ordre d'arrivée arbitraire
            return [
                {"url": "u_wide", "path": str(wide_big)},
                {"url": "u_tall", "path": str(tall_small)},
                {"url": "u_tallbig", "path": str(tall_big)},
                {"url": "u_wide", "path": str(wide_big)},   # doublon d'URL
            ]

        original_run = provider._run_crawl
        provider._run_crawl = fake_run
        try:
            got = provider.search_character_images("Lilly X", str(cand_dir), 8, 10)
        finally:
            provider._run_crawl = original_run

        urls = [g["url"] for g in got]
        check(len(urls) == len(set(urls)), "URL dupliquée éliminée")
        check(urls[0] == "u_tallbig",
              "portrait le plus grand en tête (u_tallbig)")
        check(urls[1] == "u_tall", "portrait plus petit ensuite (u_tall)")
        check(urls[2] == "u_wide", "paysage enfin (u_wide)")
        check(all(g["source"] == "google" for g in got), "moteur google renseigné")
        check(all(g["w"] and g["h"] for g in got), "dimensions jointes")
    print("[3] tri des candidats : OK")

    print("\n[4] repli Google -> Bing quand Google rend trop peu")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cand_dir = tmp / "cand"
        cand_dir.mkdir()
        only_g = _make_image(cand_dir / "g.jpg", 800, 1200)
        only_b = _make_image(cand_dir / "b.jpg", 900, 1300)

        calls = []

        def picky_run(engine_cls, parser_cls, query, out_dir, max_num, filters):
            from icrawler.builtin import BingImageCrawler
            is_bing = engine_cls is BingImageCrawler
            calls.append("bing" if is_bing else "google")
            if is_bing:
                return [{"url": "u_b", "path": str(only_b)}]
            return [{"url": "u_g", "path": str(only_g)}]

        original_run = provider._run_crawl
        provider._run_crawl = picky_run
        try:
            got = provider.search_character_images("Lilly X", str(cand_dir), 8, 10)
        finally:
            provider._run_crawl = original_run

        check(calls == ["google", "bing"], "google tenté puis bing")
        check({g["url"] for g in got} == {"u_g", "u_b"}, "images des deux moteurs conservées")
        check({g["source"] for g in got} == {"google", "bing"}, "source par image correcte")

        # Si Google suffit, on ne lance pas Bing.
        calls.clear()

        def enough_run(engine_cls, parser_cls, query, out_dir, max_num, filters):
            calls.append("bing" if engine_cls.__name__.startswith("Bing") else "google")
            return [{"url": f"u{i}", "path": str(only_g)} for i in range(6)]

        provider._run_crawl = enough_run
        try:
            # keep=6 : google en rend 6, la boucle doit s'arrêter là.
            provider.search_character_images("Lilly X", str(cand_dir), 8, 6)
        finally:
            provider._run_crawl = original_run
        check(calls == ["google"], "bing non sollicité quand google suffit")
    print("[4] repli Google -> Bing : OK")

    print("\n[5] tolérance aux pannes de moteur")
    with tempfile.TemporaryDirectory() as td:
        cand_dir = Path(td) / "cand"
        cand_dir.mkdir()

        def boom(engine_cls, parser_cls, query, out_dir, max_num, filters):
            raise RuntimeError("Google a bloqué (429)")

        original_run = provider._run_crawl
        provider._run_crawl = boom
        try:
            got = provider.search_character_images("Lilly X", str(cand_dir), 8, 10)
        finally:
            provider._run_crawl = original_run
        check(got == [], "moteurs en erreur -> liste vide, pas d'exception")

    with tempfile.TemporaryDirectory() as td:
        cand_dir = Path(td) / "cand"
        cand_dir.mkdir()
        keep = _make_image(cand_dir / "ok.jpg", 800, 1200)

        def flaky(engine_cls, parser_cls, query, out_dir, max_num, filters):
            from icrawler.builtin import BingImageCrawler
            if engine_cls is BingImageCrawler:
                return [{"url": "u_ok", "path": str(keep)}]
            raise RuntimeError("Google a bloqué (429)")

        original_run = provider._run_crawl
        provider._run_crawl = flaky
        try:
            got = provider.search_character_images("Lilly X", str(cand_dir), 8, 10)
        finally:
            provider._run_crawl = original_run
        check([g["url"] for g in got] == ["u_ok"],
              "google en panne -> bing prend le relais")
    print("[5] tolérance aux pannes : OK")

    print("\n[6] filtres et options exposés")
    check(provider.GOOGLE_FILTERS.get("type") == "photo", "filtre google type=photo")
    check(provider.GOOGLE_FILTERS.get("size") == "large", "filtre google size=large")
    check(provider.MIN_SIZE == (400, 400), "MIN_SIZE = (400, 400)")
    # Vérifié en direct : l'endpoint /images/async de Bing ne renvoie RIEN avec
    # size=large (0 résultat avec, 6 sans).
    check(provider.BING_FILTERS is None, "BING_FILTERS à None (filtre Bing cassé)")
    print("[6] filtres : OK")

    print("\n[7] parseurs : jamais None (le worker de icrawler iterate le retour)")
    class _Resp:
        def __init__(self, content):
            self.content = content

    def _parser(cls):
        """Parser icrawler : le __init__ de la base exige thread_num/signal/session,
        mais `parse()` n'utilise aucun des trois -> None suffit."""
        inst = cls.__new__(cls)
        return inst

    # Bing sérialise le JSON dans un attribut HTML : les guillemets sont donc
    # en entités `&quot;` (33 correspondance sur une requête sonde réelle).
    bing_html = (
        b'<a class="iusc" m=\'{&quot;murl&quot;:&quot;https://a.example/1.jpg&quot;,'
        b'&quot;turl&quot;:&quot;x&quot;}\'></a>'
        b'<a class="iusc" m=\'{&quot;murl&quot;:&quot;https:\\/\\/b.example\\/2.png&quot;}\'></a>'
        b'<a class="iusc" m=\'{&quot;murl&quot;:&quot;https://a.example/1.jpg&quot;}\'></a>'
    )
    tasks = _parser(provider.BingImageParser).parse(_Resp(bing_html))
    check(isinstance(tasks, list), "parseur Bing renvoie une liste")
    urls = [t["file_url"] for t in tasks]
    check(urls == ["https://a.example/1.jpg", "https://b.example/2.png"],
          f"URLs Bing extraites et dédoublonnées: {urls}")
    check(_parser(provider.BingImageParser).parse(_Resp(b"")) == [],
          "parseur Bing sur page vide -> [] et non None")
    check(_parser(provider.BingImageParser).parse(_Resp(b"\xff\xfe pas du html")) == [],
          "parseur Bing sur bytes invalides -> [] et non None")
    check(isinstance(_parser(provider.GoogleImageParser).parse(_Resp(b"")), list),
          "parseur Google renvoie une liste")
    check(isinstance(_parser(provider.GoogleImageParser).parse(_Resp(None)), list),
          "parseur Google sur contenu None -> [] et non None")
    print("[7] parseurs : OK")

    print("\n[8] chemin complet enregistré (régression)")
    # `task["filename"]` est un nom RELATIF au root_dir. En le stockant tel quel,
    # `_probe` ne trouvait pas le fichier et TOUS les candidats étaient rejetés :
    # la recherche renvoyait 0 image sans lever la moindre erreur.
    import icrawler.downloader as _dlmod
    from icrawler.builtin import BingImageCrawler

    crawler = BingImageCrawler(
        parser_cls=provider.BingImageParser,
        downloader_cls=provider._RecordingDownloader,
        storage={"root_dir": "/tmp/jev_root_inexistant"},
    )
    original_download = _dlmod.ImageDownloader.download
    _dlmod.ImageDownloader.download = (
        lambda self, task, *a, **k: task.update({"success": True, "filename": "000001.jpg"})
    )
    try:
        crawler.downloader.download({"file_url": "https://example.com/a.jpg"})
    finally:
        _dlmod.ImageDownloader.download = original_download
    check(len(crawler.downloader.records) == 1, "download enregistré")
    check(crawler.downloader.records[0]["path"] == "/tmp/jev_root_inexistant/000001.jpg",
          f"chemin complet (root_dir + filename): {crawler.downloader.records[0]['path']}")
    check(os.path.isabs(crawler.downloader.records[0]["path"]),
          "chemin absolu, donc trouvable par _probe")
    print("[8] chemin complet : OK")

    if failures:
        print(f"\nECHECS : {len(failures)}")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\nTOUS LES TESTS PASSENT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())