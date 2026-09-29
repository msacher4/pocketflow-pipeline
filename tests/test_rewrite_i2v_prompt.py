"""Test du node RewriteI2VPromptNode.

Vérifie que les prompts des slots I2V sont réécrits APRÈS la sélection de
l'image réelle, en s'appuyant sur ce que voit le modèle vision (mocké ici) :
- le prompt du slot i2v est remplacé par la réécriture du modèle vision ;
- les slots non-i2v sont laissés intacts ;
- un slot sans image ou sans prompt reste inchangé (pas d'échec).
Aucun LLM, aucun GPU — appel vision mocké.
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

from nodes.assetfinder.rewrite_i2v_prompt import RewriteI2VPromptNode

NODE = "nodes.assetfinder.rewrite_i2v_prompt"


def _shared(pipeline_id, slots, tmp):
    img1 = os.path.join(tmp, "img_1.webp")
    img2 = os.path.join(tmp, "img_2.webp")
    for p in (img1, img2):
        open(p, "wb").write(bytes([0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x45, 0x42, 0x50]))
    return {
        "pipeline_id": pipeline_id,
        "asset_blueprint": {"slots": slots},
        "generated_images": [
            {"slot_id": 1, "image_path": img1, "confirmed": True},
            {"slot_id": 2, "image_path": img2, "confirmed": True},
        ],
        "_traces": {},
    }


def _slot(slot_id, mode="i2v", prompt=None):
    return {
        "id": slot_id,
        "section": "hook" if slot_id == 1 else "body",
        "position": slot_id - 1,
        "type": "visual",
        "mode": mode,
        "content": "visual desc",
        "prompt": prompt,
    }


async def _run(slots, fake_vision_output):
    node = RewriteI2VPromptNode()
    pipeline_id = f"test-rewrite-{os.getpid()}"
    tmp = tempfile.mkdtemp()
    shared = _shared(pipeline_id, slots, tmp)

    class _FakeVision:
        async def __call__(self, *a, **k):
            class _Resp:
                def __init__(self):
                    self._status = None
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"choices": [{"message": {"content": fake_vision_output}}]}
            return _Resp()

    with (
        patch(f"{NODE}._find_image_for_slot", side_effect=lambda sh, sid: sh["generated_images"][sid - 1]["image_path"]),
        patch(f"{NODE}.load_soul", return_value="soul"),
        patch(f"{NODE}.httpx.AsyncClient") as mock_client,
    ):
        mock_client.return_value.__aenter__.return_value.post = _FakeVision()
        exec_result = json.loads(await node.exec_async(shared))

    return shared, exec_result


def test_prompt_i2v_reecrit_dans_blueprint():
    slots = [
        _slot(1, prompt="A video of robin in action"),
        _slot(2, prompt="A video of robin dynamic pose"),
        _slot(3, mode="t2v", prompt="A t2v unrelated prompt"),
    ]
    new_prompt = "Real image: robin seated with flowers, subtle hair sway, gentle dolly-in"
    shared, out = asyncio.run(_run(slots, new_prompt))

    assert len(out["rewritten"]) == 2, out["rewritten"]
    rewritten_ids = {r["slot_id"] for r in out["rewritten"]}
    assert rewritten_ids == {1, 2}, rewritten_ids

    # Les slots I2V ont le nouveau prompt
    assert shared["asset_blueprint"]["slots"][0]["prompt"] == new_prompt
    assert shared["asset_blueprint"]["slots"][1]["prompt"] == new_prompt

    # Le slot T2V (mode != i2v) est intact
    assert shared["asset_blueprint"]["slots"][2]["prompt"] == "A t2v unrelated prompt"

    print("test_prompt_i2v_reecrit_dans_blueprint PASSED")


def test_slot_sans_prompt_reste_inchange():
    slots = [
        _slot(1, prompt="valid prompt"),
        _slot(2, prompt=""),
    ]
    shared, out = asyncio.run(_run(slots, "rewritten prompt"))

    assert len(out["rewritten"]) == 1, out["rewritten"]
    assert shared["asset_blueprint"]["slots"][1]["prompt"] == ""
    print("test_slot_sans_prompt_reste_inchange PASSED")


def test_aucun_slot_i2v():
    slots = [_slot(3, mode="t2v", prompt="t2v prompt")]
    shared, out = asyncio.run(_run(slots, "should not be used"))

    assert out["rewritten"] == []
    assert shared["asset_blueprint"]["slots"][0]["prompt"] == "t2v prompt"
    print("test_aucun_slot_i2v PASSED")


if __name__ == "__main__":
    test_prompt_i2v_reecrit_dans_blueprint()
    test_slot_sans_prompt_reste_inchange()
    test_aucun_slot_i2v()