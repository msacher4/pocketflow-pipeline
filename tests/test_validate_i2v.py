#!/usr/bin/env python3
"""Tests unitaires de ValidateI2V (validation TG image + prompt réécrit).

Teste : envoi des messages, résolution approve/reject via callbacks, rebouclage
ciblé (regen_image / regen_prompt), timeout -> auto-approve.

Usage:
  python3 tests/test_validate_i2v.py
"""
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nodes.assetfinder.validate_i2v as vmod
from nodes.assetfinder.validate_i2v import ValidateI2V
from helpers.state import _register_validation, _pending_validations, _resolve_validation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("test-validate-i2v")

SENT = []


async def fake_send_photo(path, caption, buttons=None):
    SENT.append({"kind": "image", "path": path, "caption": caption, "buttons": buttons})
    return len(SENT)


async def fake_send_telegram(text, buttons=None):
    SENT.append({"kind": "prompt", "text": text, "buttons": buttons})
    return len(SENT)


vmod.send_photo_tg = fake_send_photo
vmod.send_telegram = fake_send_telegram


def build_shared(pipeline_id="vli2v"):
    return {
        "pipeline_id": pipeline_id,
        "steps": [],
        "_traces": {},
        "asset_blueprint": {
            "slots": [
                {"id": 1, "mode": "i2v", "section": "HOOK", "position": 0,
                 "content": "hero push-ups",
                 "prompt": "PROMPT REECRIT 1"},
                {"id": 2, "mode": "i2v", "section": "BODY", "position": 1,
                 "content": "smoothie drink",
                 "prompt": "PROMPT REECRIT 2"},
            ],
        },
        "generated_images": [
            {"slot_id": 1, "image_path": "/fake/path/img1.jpg", "confirmed": True},
            {"slot_id": 2, "image_path": "/fake/path/img2.jpg", "confirmed": True},
        ],
    }


async def resolve_all(shared, results):
    """Résout les callbacks pour chaque vid présent."""
    vids = list(_pending_validations.keys())
    for i, vid in enumerate(vids):
        result = results[i] if i < len(results) else "approve"
        _resolve_validation(vid, result)
    return vids


def run(node, shared):
    async def inner():
        await node.prep_async(shared)
        exec = await node.exec_async(shared)
        action = await node.post_async(shared, None, exec)
        return json.loads(exec), action
    return inner


def test_tout_approuve():
    SENT.clear()
    shared = build_shared()
    node = ValidateI2V()

    async def scenario():
        task = asyncio.create_task(run(node, shared)())
        await asyncio.sleep(0.05)
        vids = list(_pending_validations.keys())
        assert len(vids) == 4, f"attendu 4 vids (2 img + 2 prm), got {vids}"
        for v in vids:
            _resolve_validation(v, "approve")
        (data, action) = await task
        return data, action

    data, action = asyncio.run(scenario())
    assert action == "default", f"connu default (approve), got {action}"
    assert data["rejected_images"] == []
    assert data["rejected_prompts"] == []
    assert len(SENT) == 4
    kinds = sorted(s["kind"] for s in SENT)
    assert kinds == ["image", "image", "prompt", "prompt"]
    _pending_validations.clear()
    log.info("PASSED")


def test_image_rejetee():
    SENT.clear()
    shared = build_shared()
    node = ValidateI2V()

    async def scenario():
        task = asyncio.create_task(run(node, shared)())
        await asyncio.sleep(0.05)
        vids = list(_pending_validations.keys())
        for v in vids:
            if "i2v_img_1" in v:
                _resolve_validation(v, "reject")
            else:
                _resolve_validation(v, "approve")
        (data, action) = await task
        return data, action

    data, action = asyncio.run(scenario())
    assert action == "regen_image", f"connu regen_image, got {action}"
    assert data["rejected_images"] == [1], data["rejected_images"]
    assert 1 in data["rejected_prompts"], "image rejetée => prompt aussi à refaire"
    assert shared["_i2v_regen_image_slots"] == [1]
    assert 1 in shared["_i2v_regen_prompt_slots"]
    _pending_validations.clear()
    log.info("PASSED")


def test_prompt_rejete():
    SENT.clear()
    shared = build_shared()
    node = ValidateI2V()

    async def scenario():
        task = asyncio.create_task(run(node, shared)())
        await asyncio.sleep(0.05)
        vids = list(_pending_validations.keys())
        for v in vids:
            if "i2v_prm_2" in v:
                _resolve_validation(v, "reject")
            else:
                _resolve_validation(v, "approve")
        (data, action) = await task
        return data, action

    data, action = asyncio.run(scenario())
    assert action == "regen_prompt", f"connu regen_prompt, got {action}"
    assert data["rejected_images"] == []
    assert data["rejected_prompts"] == [2], data["rejected_prompts"]
    assert shared["_i2v_regen_image_slots"] == []
    assert shared["_i2v_regen_prompt_slots"] == [2]
    _pending_validations.clear()
    log.info("PASSED")


def test_timeout_auto_approve():
    SENT.clear()
    shared = build_shared()
    node = ValidateI2V(timeout=0.1)

    async def scenario():
        (data, action) = await run(node, shared)()
        return data, action

    data, action = asyncio.run(scenario())
    assert action == "default", f"connu default (timeout), got {action}"
    assert data["rejected_images"] == []
    _pending_validations.clear()
    log.info("PASSED")


def test_cablage_flow_alt():
    """Le graphe alt doit enchaîner validate_i2v -> cleanup -> ... -> svg (jamais s'arrêter sur approve).

    Scénario réel du bug : post_async retournait "approve", or seul l'edge
    "default" existe depuis validate_i2v -> la boucle s'arrêtait et le pipeline
    rendait partial. De retour "default", le cablage existant mène jusqu'au SVG.
    """
    from nodes.assetfinder import build_assetfinder_alt_flow
    from nodes.assetfinder.rewrite_i2v_prompt import RewriteI2VPromptNode
    from nodes.assetfinder.validate_i2v import ValidateI2V

    flow = build_assetfinder_alt_flow()
    start_node = flow.start_node

    def _step_of(node):
        return (getattr(node, "step_name", None) or getattr(node, "step", None)
                or node.__class__.__name__)

    def _succ_targets(succ):
        for name, target in (succ or {}).items():
            yield name, target

    visited = {}
    seen = set()

    def probe(node):
        if node in seen:
            return
        seen.add(node)
        step = _step_of(node)
        succ = getattr(node, "successors", {})
        default = succ.get("default")
        visited[step] = [_step_of(default)] if default else []
        for name, target in succ.items():
            targets = target if isinstance(target, list) else [target]
            for t in targets:
                probe(t)

    probe(start_node)

    assert "validate_i2v" in visited, f"validate_i2v absent du graphe: {list(visited)}"
    assert visited["validate_i2v"], "validate_i2v doit avoir un successeur default (cleanup)"
    assert "cleanup" in visited["validate_i2v"][0].lower(), visited["validate_i2v"]

    reachable = []

    def walk(node, seen_nodes=None):
        seen_nodes = seen_nodes or set()
        if node in seen_nodes:
            return
        seen_nodes.add(node)
        reachable.append(_step_of(node))
        for name, target in getattr(node, "successors", {}).items():
            targets = target if isinstance(target, list) else [target]
            for t in targets:
                walk(t, seen_nodes)

    walk(start_node)
    assert "SDCppVideoGenerator" in reachable, f"svg jamais atteint depuis validate_i2v: {reachable}"
    log.info(f"PASSED (validate_i2v -> {visited['validate_i2v'][0]} -> ... -> svg)")

    _pending_validations.clear()


if __name__ == "__main__":
    test_tout_approuve()
    test_image_rejetee()
    test_prompt_rejete()
    test_timeout_auto_approve()
    test_cablage_flow_alt()
    log.info("ALL PASSED")