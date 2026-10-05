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
    # Le cleanup llama est desormais place AVANT klein_ref (ComfyUI charge
    # ~15.9 Go et n'a pas la place de cohabiter avec le LLM) : le successeur
    # de validate_i2v est donc free_svg, et c'est real -> cleanup qui porte
    # l_assertion (verifie plus bas).
    assert visited["validate_i2v"], "validate_i2v doit avoir un successeur default (free_svg)"
    assert "free_svg" in visited["validate_i2v"][0].lower(), visited["validate_i2v"]

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


def test_cleanup_avant_klein_ref():
    """Le cleanup doit s'executer AVANT klein_ref, pas apres validate_i2v.

    ComfyUI (Klein 4B) monte a ~15.9 Go sur 16 Go : avec les LLM residents la
    cohabitation echoue. Il faut donc tomber les DEUX serveurs — llama-proxy 8080
    ET Jev-Omni 8977 — d'ou CleanupLlamaJev plutot que CleanupLlamaProxy.
    `real` reste avant le cleanup car il reveille Jev-Omni, et rewrite_i2v_prompt
    reste apres car il reveille le modele vision a la demande.
    """
    from nodes.assetfinder import build_assetfinder_alt_flow

    flow = build_assetfinder_alt_flow()
    ap = flow.start_node.successors["default"]
    real = ap.successors["default"]

    def _name(node):
        return type(node).__name__

    assert _name(real) == "RealCharacterImageNode", _name(real)

    # validate_refs est entre real et le cleanup : dernier filet humain avant
    # de lancer Klein.
    validate_refs = real.successors.get("default")
    assert validate_refs is not None and _name(validate_refs) == "ValidateCharacterRefs", real.successors

    # CleanupLlamaJev et NON CleanupLlamaProxy : le proxy seul laisserait
    # Jev-Omni charge (~7 Go) pendant que Klein monte a ~15.9 Go sur 16 Go.
    cleanup = validate_refs.successors.get("default")
    assert cleanup is not None and _name(cleanup) == "CleanupLlamaJev", validate_refs.successors

    klein = cleanup.successors.get("default")
    assert klein is not None and _name(klein) == "ComfyUIKleinRefImageGenerator", cleanup.successors
    log.info("PASSED (real -> validate_refs -> CleanupLlamaJev -> klein_ref)")


def test_retry_no_image_terminal():
    """real -"retry_no_image" doit mener a un noeud TERMINAL (sinon Flow ends).

    Sans cet edge, un slot I2V sans image Danbooru arretait le sous-flux sur
    `Flow ends: 'retry_no_image' not found in ['default']` : le run mourait
    en silence. Le noeud rend l'action que le parent route vers l'ActuFinder.
    """
    from nodes.assetfinder import build_assetfinder_alt_flow
    from nodes.assetfinder.retry_no_image import RetryNoImage

    flow = build_assetfinder_alt_flow()
    ap = flow.start_node.successors["default"]
    real = ap.successors["default"]

    assert "retry_no_image" in real.successors, sorted(real.successors)
    retry_node = real.successors["retry_no_image"]
    assert isinstance(retry_node, RetryNoImage), type(retry_node)
    # Terminal : aucun successeur, donc le sous-flux se termine ici et rend
    # l'action "retry_no_image" au parent (et non une fin de flow muette).
    assert not getattr(retry_node, "successors", {}), retry_node.successors

    # ... et le noeud ne doit surtout PAS poser _error : ValidationSubFlowNode
    # transformerait alors l'action en "error" et le rebouclage ActuFinder
    # serait perdu.
    import asyncio
    shared = {"steps": [], "_missing_image_slots": [1, 2]}
    action = asyncio.run(RetryNoImage().run_async(shared))
    assert action == "retry_no_image", action
    assert "_error" not in shared, shared.get("_error")
    assert [s["step"] for s in shared["steps"]] == ["retry_no_image"], shared["steps"]
    log.info("PASSED (retry_no_image terminal, sans _error)")


def test_alt_retry_gate_borne():
    """AltRetryGate doit plafonner a ALT_RETRY_MAX puis exposer l'echec.

    L'edge "give_up" n'etait cable vers rien : apres ALT_RETRY_MAX articles
    sans image reelle, le pipeline s'arretait sans message. Le gate doit
    now poser _error + notifier avant de rendre "give_up".
    """
    import importlib
    import asyncio

    main_mod = importlib.import_module("main")
    gate = main_mod.AltRetryGate()

    # Stub : sans ce patch le give_up envoie un VRAI message Telegram.
    sent = []

    async def _fake_tg(text, buttons=None):
        sent.append(text)
        return 1

    main_mod.send_telegram = _fake_tg

    async def _run_until_give_up():
        # Un SEUL shared : le compteur _alt_retry_count vit dans le shared du
        # run et survit au rebouclage vers l'ActuFinder.
        shared = {"steps": []}
        return [await gate.run_async(shared) for _ in range(main_mod.ALT_RETRY_MAX + 1)]

    actions = asyncio.run(_run_until_give_up())
    assert actions[:main_mod.ALT_RETRY_MAX] == ["retry"] * main_mod.ALT_RETRY_MAX, actions
    assert actions[-1] == "give_up", actions
    # L'echec doit etre visible : une notif Telegram a partir du 4e article.
    assert len(sent) == 1, sent
    assert str(main_mod.ALT_RETRY_MAX) in sent[0], sent[0]
    log.info(f"PASSED (ALT_RETRY_MAX={main_mod.ALT_RETRY_MAX} -> give_up notifié)")


if __name__ == "__main__":
    test_tout_approuve()
    test_image_rejetee()
    test_prompt_rejete()
    test_timeout_auto_approve()
    test_cablage_flow_alt()
    test_cleanup_avant_klein_ref()
    test_retry_no_image_terminal()
    test_alt_retry_gate_borne()
    log.info("ALL PASSED")