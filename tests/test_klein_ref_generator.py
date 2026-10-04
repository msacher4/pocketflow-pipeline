"""Test de ComfyUIKleinRefImageGenerator (Klein 4B + références Danbooru).

ComfyUI est simulé : on teste le contrat du nœud et surtout le PIÈGE de priorité
de `_find_image_for_slot`, qui privilégie la première entrée `confirmed`.

Scénario couvert : RealCharacterImageNode laisse une entrée `confirmed: True`
(image Danbooru). Si le nœud Klein AJOUTE son entrée au lieu de REMPLACER, le
downstream continue d'animer le Danbooru et la feature ne sert à rien.
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

from nodes.assetfinder.comfyui_klein_ref_generator import (
    ComfyUIKleinRefImageGenerator,
    _reference_paths_for_slot,
)
from nodes.assetfinder.sdcpp_i2v_generator import _find_image_for_slot

REFS = ["/tmp/danbooru_a.img", "/tmp/danbooru_b.img"]


def _shared(slots=None, generated=None):
    return {
        "pipeline_id": "test-klein-ref",
        "topic": "test",
        "asset_blueprint": {
            "slots": slots if slots is not None else [
                {"id": 1, "section": "hook", "position": 0, "mode": "i2v",
                 "prompt": "portrait of Aiko, sitting on a bench", "content": "c1"},
                {"id": 2, "section": "body", "position": 1, "mode": "i2v",
                 "prompt": "portrait of Aiko, walking in the city", "content": "c2"},
            ]
        },
        "generated_images": generated if generated is not None else [
            {"slot_id": 1, "prompt": "p", "image_path": REFS[0],
             "reference_paths": list(REFS), "confirmed": True},
            {"slot_id": 2, "prompt": "p", "image_path": REFS[0],
             "reference_paths": list(REFS), "confirmed": True},
        ],
        "steps": [],
        "_traces": {},
    }


def _danbooru_files():
    """Les chemins de référence doivent exister pour comfyui_generate_image_ref."""
    for p in REFS:
        Path(p).write_bytes(b"x")


async def _run(shared, out_names):
    node = ComfyUIKleinRefImageGenerator()
    calls = []

    async def fake_ref(*a, **kw):
        calls.append(kw)
        name = kw["name"]
        return f"/out/{name}.png" if name in out_names else None

    with (
        patch("nodes.assetfinder.comfyui_klein_ref_generator.DOWNLOADS_DIR", Path(tempfile.mkdtemp())),
        patch("nodes.assetfinder.comfyui_klein_ref_generator.comfyui_ensure_started", new=AsyncMock()),
        patch("nodes.assetfinder.comfyui_klein_ref_generator.comfyui_generate_image_ref", new=fake_ref),
        patch("nodes.assetfinder.comfyui_klein_ref_generator._set_state", new=AsyncMock()),
        patch("nodes.assetfinder.comfyui_klein_ref_generator._set_traces", new=AsyncMock()),
    ):
        prep = await node.prep_async(shared)
        result = json.loads(await node.exec_async(shared))
        await node.post_async(shared, prep, json.dumps(result))
    return calls


def test_find_image_returns_klein_not_danbooru():
    """LE test critique : après Klein, l'I2V doit cibler l'image Klein."""
    _danbooru_files()
    shared = _shared()
    asyncio.run(_run(shared, {"klein_1", "klein_2"}))

    for sid in (1, 2):
        got = _find_image_for_slot(shared, sid)
        assert got == f"/out/klein_{sid}.png", (sid, got)
    print("test_find_image_returns_klein_not_danbooru PASSED")


def test_confirmed_danbooru_entry_disparue():
    """Plus aucune entrée confirmed: True ne doit subsister pour ces slots,
    sinon la priorité de _find_image_for_slot rebasculerait sur le Danbooru."""
    _danbooru_files()
    shared = _shared()
    asyncio.run(_run(shared, {"klein_1", "klein_2"}))

    for img in shared["generated_images"]:
        assert not img["confirmed"], img
        assert img["reference_paths"] == REFS, img
    print("test_confirmed_danbooru_entry_disparue PASSED")


def test_references_forwarded():
    _danbooru_files()
    shared = _shared()
    calls = asyncio.run(_run(shared, {"klein_1", "klein_2"}))

    assert len(calls) == 2, calls
    assert all(c["reference_images"] == REFS for c in calls), calls
    # Résolution = cible I2V, sinon l'image de source ne matche pas la base I2V.
    assert all(c["width"] == 320 and c["height"] == 576 for c in calls), calls
    print("test_references_forwarded PASSED")


def test_slot_sans_reference_ignore():
    """Pas de référence -> slot ignoré avec erreur explicite, pas de génération
    silencieuse en text-to-image (qui perdrait le personnage)."""
    shared = _shared()
    shared["generated_images"] = [
        {"slot_id": 1, "image_path": "/tmp/x.img", "reference_paths": list(REFS), "confirmed": True},
        {"slot_id": 2, "image_path": "/tmp/y.img", "confirmed": True},
    ]
    asyncio.run(_run(shared, {"klein_1"}))

    # slot 2 non généré -> son entrée Danbooru doit rester
    kept = {img["slot_id"] for img in shared["generated_images"]}
    assert kept == {1, 2}, kept
    assert _find_image_for_slot(shared, 2) == "/tmp/y.img"
    print("test_slot_sans_reference_ignore PASSED")


def test_regen_ne_touche_qu_un_slot():
    """Régénération ciblée : l'autre slot conserve son image Klein."""
    _danbooru_files()
    shared = _shared()
    shared["_i2v_regen_image_slots"] = [2]
    shared["generated_images"] = [
        {"slot_id": 1, "image_path": "/out/klein_1.png", "reference_paths": list(REFS), "confirmed": False},
        {"slot_id": 2, "image_path": REFS[0], "reference_paths": list(REFS), "confirmed": True},
    ]
    asyncio.run(_run(shared, {"klein_2"}))

    assert _find_image_for_slot(shared, 1) == "/out/klein_1.png"
    assert _find_image_for_slot(shared, 2) == "/out/klein_2.png"
    print("test_regen_ne_touche_qu_un_slot PASSED")


def test_reference_paths_helper_accepte_legacy_str():
    """Robustesse : une entrée dont reference_paths est une string (format
    d'une seule image) doit rester lisible."""
    shared = _shared(generated=[{"slot_id": 9, "reference_paths": "/tmp/only.img"}])
    assert _reference_paths_for_slot(shared, 9) == ["/tmp/only.img"]
    assert _reference_paths_for_slot(shared, 404) == []
    print("test_reference_paths_helper_accepte_legacy_str PASSED")


if __name__ == "__main__":
    test_reference_paths_helper_accepte_legacy_str()
    test_find_image_returns_klein_not_danbooru()
    test_confirmed_danbooru_entry_disparue()
    test_references_forwarded()
    test_slot_sans_reference_ignore()
    test_regen_ne_touche_qu_un_slot()
    print("\nTous les tests du nœud Klein ref sont passés.")