"""Test du merge de ComfyUIImageGenerator.post_async.

Régression : `prev` était lu APRÈS l'affectation de `shared["generated_images"]`.
`kept` ressortait donc toujours vide et une régénération partielle effaçait les
images de TOUS les autres slots. ComfyUI est simulé ici, aucun GPU.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(PIPELINE_ROOT / "nodes"))

from nodes.assetfinder.comfyui_image_generator import ComfyUIImageGenerator


def _slots(n=3):
    return [{"id": i, "section": "body", "position": i, "prompt": f"prompt {i}",
             "content": f"c{i}", "expected": ""} for i in range(1, n + 1)]


def _shared(slots=None, generated=None, regen=None):
    return {
        "pipeline_id": "test-img-gen-merge",
        "topic": "test",
        "asset_blueprint": {"slots": slots if slots is not None else _slots()},
        "generated_images": generated if generated is not None else [
            {"slot_id": i, "image_path": f"/old/klein_{i}.png", "confirmed": False}
            for i in (1, 2, 3)
        ],
        "steps": [],
        "_traces": {},
        **({"_slots_to_regenerate": regen} if regen else {}),
    }


async def _run(shared):
    node = ComfyUIImageGenerator()

    async def fake_img(*a, **kw):
        return f"/new/{kw['name']}.png"

    with (
        patch("nodes.assetfinder.comfyui_image_generator.DOWNLOADS_DIR", Path(tempfile.mkdtemp())),
        patch("nodes.assetfinder.comfyui_image_generator.comfyui_ensure_started", new=AsyncMock()),
        patch("nodes.assetfinder.comfyui_image_generator.comfyui_generate_image", new=fake_img),
        patch("nodes.assetfinder.comfyui_image_generator._set_state", new=AsyncMock()),
        patch("nodes.assetfinder.comfyui_image_generator._set_traces", new=AsyncMock()),
    ):
        prep = await node.prep_async(shared)
        result = json.loads(await node.exec_async(shared))
        await node.post_async(shared, prep, json.dumps(result))
    return shared


def test_regen_partielle_conserve_les_autres_slots():
    """LE test de régression : regen du slot 2 seul, les slots 1 et 3 survivent."""
    shared = _shared(regen=[2])
    asyncio.run(_run(shared))

    by_slot = {}
    for img in shared["generated_images"]:
        by_slot.setdefault(img["slot_id"], []).append(img["image_path"])

    assert sorted(by_slot) == [1, 2, 3], by_slot
    assert by_slot[1] == ["/old/klein_1.png"], by_slot
    assert by_slot[2] == ["/new/img_2.png"], by_slot
    assert by_slot[3] == ["/old/klein_3.png"], by_slot
    print("test_regen_partielle_conserve_les_autres_slots PASSED")


def test_aucune_doublon_pour_le_slot_regenere():
    """Le slot régénéré ne doit pas garder son ancienne entrée en doublon."""
    shared = _shared(regen=[2])
    asyncio.run(_run(shared))

    slot2 = [i for i in shared["generated_images"] if i["slot_id"] == 2]
    assert len(slot2) == 1, slot2
    assert "/old/" not in slot2[0]["image_path"], slot2
    print("test_aucune_doublon_pour_le_slot_regenere PASSED")


def test_regen_de_plusieurs_slots():
    """Deux slots régénérés d'un coup : le troisième est préservé, sans doublon."""
    shared = _shared(regen=[1, 3])
    asyncio.run(_run(shared))

    by_slot = {}
    for img in shared["generated_images"]:
        by_slot.setdefault(img["slot_id"], []).append(img["image_path"])
    assert sorted(by_slot) == [1, 2, 3], by_slot
    assert by_slot[1] == ["/new/img_1.png"], by_slot
    assert by_slot[2] == ["/old/klein_2.png"], by_slot
    assert by_slot[3] == ["/new/img_3.png"], by_slot
    print("test_regen_de_plusieurs_slots PASSED")


def test_regen_complete_regenere_tout():
    """Sans _slots_to_regenerate, tous les slots passent : rien à conserver."""
    shared = _shared(regen=None)
    asyncio.run(_run(shared))

    paths = {i["slot_id"]: i["image_path"] for i in shared["generated_images"]}
    assert paths == {1: "/new/img_1.png", 2: "/new/img_2.png", 3: "/new/img_3.png"}, paths
    print("test_regen_complete_regenere_tout PASSED")


if __name__ == "__main__":
    test_regen_partielle_conserve_les_autres_slots()
    test_aucune_doublon_pour_le_slot_regenere()
    test_regen_de_plusieurs_slots()
    test_regen_complete_regenere_tout()
    print("\nTous les tests du merge ComfyUIImageGenerator sont passés.")