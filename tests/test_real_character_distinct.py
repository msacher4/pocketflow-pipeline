"""Test de la distinctivité des images réelles I2V par personnage.

Vérifie que sur N slots I2V du MÊME personnage, chaque slot obtient une image
DIFFÉRENTE (jamais 2x le même file_url). Cas couverts, sans LLM ni GPU :
- 2 slots I2V, LLM qui choisit la même URL pour les 2 -> le fallback distinct
  retient une 2e image pour le slot 2 (2 images différentes).
- 1 seul post dispo -> le slot 2 tombe en missing_ids (comportement existant).
- _fallback_choice exclut les URLs déjà utilisées.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(PIPELINE_ROOT / "nodes"))

from nodes.assetfinder.real_character_image import RealCharacterImageNode


CHARACTER = {"name": "Robin", "franchise": "Honkai: Star Rail"}

POSTS = {
    "https://cdn1/img_a.png": {"id": 1, "file_url": "https://cdn1/img_a.png", "w": 1000, "h": 1500, "tags": "robin 1girl"},
    "https://cdn1/img_b.png": {"id": 2, "file_url": "https://cdn1/img_b.png", "w": 900, "h": 1600, "tags": "robin 1girl"},
    "https://cdn1/img_c.png": {"id": 3, "file_url": "https://cdn1/img_c.png", "w": 800, "h": 1200, "tags": "robin 1girl"},
}


def _shared(pipeline_id):
    return {
        "pipeline_id": pipeline_id,
        "topic": "test",
        "asset_blueprint": {
            "slots": [
                {"id": 1, "section": "hook", "position": 0, "type": "visual",
                 "mode": "i2v", "character": CHARACTER, "content": "Robin in action", "prompt": "p1"},
                {"id": 2, "section": "body", "position": 1, "type": "visual",
                 "mode": "i2v", "character": CHARACTER, "content": "Robin dynamic pose", "prompt": "p2"},
            ]
        },
        "_traces": {},
    }


class _FakeLLMChoice:
    """Mock du LLM : choisit TOUJOURS la 1ère URL (même image pour tous les slots)."""

    def __init__(self, file_url):
        self._url = file_url

    async def __call__(self, *a, **k):
        return json.dumps({"images": [{"file_url": self._url, "w": 1000, "h": 1500}]})


async def _run(posts_list, llm_choice):
    node = RealCharacterImageNode()
    pipeline_id = f"test-real-char-{os.getpid()}"
    shared = _shared(pipeline_id)
    with (
        patch("nodes.assetfinder.real_character_image.search_posts", return_value=posts_list),
        patch("nodes.assetfinder.real_character_image.call_llm", new=_FakeLLMChoice(llm_choice)),
        patch("nodes.assetfinder.real_character_image.download_image",
              side_effect=lambda url, dest: (_write(dest, url) or dest)),
        patch("nodes.assetfinder.real_character_image.load_soul", return_value="soul"),
    ):
        exec_result = json.loads(await node.exec_async(shared))
    return exec_result


def _write(dest, url):
    open(dest, "wb").write(bytes(url, "utf-8"))


def _posts(list_of_posts):
    return [dict(POSTS[url]) for url in list_of_posts]


def test_deux_slots_i2v_images_distinctes():
    """LLM qui pointe tjrs la même URL : le slot 2 récupère quand même une image
    différente via l'exclusion (fallback ou candidats filtrés)."""
    tmp = tempfile.mkdtemp()
    with patch("nodes.assetfinder.real_character_image.DOWNLOADS_DIR", Path(tmp)):
        out = asyncio.run(_run(_posts(["https://cdn1/img_a.png", "https://cdn1/img_b.png", "https://cdn1/img_c.png"]),
                               "https://cdn1/img_a.png"))

    imgs = out["generated_images"]
    assert len(imgs) == 2, imgs
    assert not out["missing_ids"], out["missing_ids"]

    paths = {os.path.basename(g["image_path"]) for g in imgs}
    assert paths == {"char_1.img", "char_2.img"}, paths

    # Deux fichiers réellement différents (contenu différent).
    contents = set()
    for g in imgs:
        contents.add(open(g["image_path"], "rb").read())
    assert len(contents) == 2, "2 slots i2v -> 2 images distinctes attendues"

    print("test_deux_slots_i2v_images_distinctes PASSED")


def test_un_seul_post_slot2_missing():
    """Un seul post disponible : le slot 2 n'a pas d'image -> missing_ids (pas de
    duplicata, pas de bricolage)."""
    tmp = tempfile.mkdtemp()
    with patch("nodes.assetfinder.real_character_image.DOWNLOADS_DIR", Path(tmp)):
        out = asyncio.run(_run(_posts(["https://cdn1/img_a.png"]), "https://cdn1/img_a.png"))

    assert len(out["generated_images"]) == 1
    assert out["missing_ids"] == [2], out["missing_ids"]
    print("test_un_seul_post_slot2_missing PASSED")


def test_fallback_choice_exclut_used():
    node = RealCharacterImageNode()
    posts = _posts(["https://cdn1/img_a.png", "https://cdn1/img_b.png"])
    assert node._fallback_choice(posts, {"https://cdn1/img_a.png"})["file_url"] == "https://cdn1/img_b.png"
    assert node._fallback_choice(posts, {"https://cdn1/img_a.png", "https://cdn1/img_b.png"}) is None
    print("test_fallback_choice_exclut_used PASSED")


if __name__ == "__main__":
    test_fallback_choice_exclut_used()
    test_deux_slots_i2v_images_distinctes()
    test_un_seul_post_slot2_missing()