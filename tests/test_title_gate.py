#!/usr/bin/env python3
"""Tests du TitleCharacterGateNode.

Vérifie que le gate :
- renvoie "approve" quand un perso est nommé et enrichit selected_article ;
- renvoie "no_good_news" quand aucun perso n'est identifiable et marque l'URL used ;
- détecte proprement un truc générique sans perso (le cas Wolverine/hololive).

Usage:
  python3 tests/test_title_gate.py
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nodes.actufinder.title_gate as tmod
from nodes.actufinder.title_gate import TitleCharacterGateNode
from nodes.actufinder.used_articles import clear_used, is_used

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("test-title-gate")

_RESPONSES = []


async def fake_call_llm(model, system, user_msg, max_tokens=4096, timeout=180):
    return _RESPONSES.pop(0)


tmod.call_llm = fake_call_llm


def build_shared():
    clear_used()
    return {
        "pipeline_id": "tgate",
        "steps": [],
        "_traces": {},
        "topic": "Test",
        "selected_article": {
            "title": "Mai Shiranui returns in KOF XV trailer",
            "url": "https://src/kof1", "source": "src",
            "score": 91, "hook_angle": "The detail everyone missed",
        },
    }


def test_perso_nomme():
    shared = build_shared()
    _RESPONSES[:] = [
        json.dumps({"has_character": True, "character_name": "Mai Shiranui",
                    "franchise": "King of Fighters"})
    ]

    async def scenario():
        return await TitleCharacterGateNode().post_async(
            shared, None, await TitleCharacterGateNode().exec_async(shared))

    action = asyncio.run(scenario())
    assert action == "approve", action
    assert shared["selected_article"]["character_name"] == "Mai Shiranui"
    assert shared["selected_article"]["franchise"] == "King of Fighters"
    log.info("PASSED")


def test_aucun_perso():
    shared = build_shared()
    shared["selected_article"] = {
        "title": "God of War Creator Slams Female Characters in Marvel's Wolverine",
        "url": "https://src/wolv", "source": "src",
        "score": 88, "hook_angle": "The creator slams female designs",
    }
    _RESPONSES[:] = [json.dumps({"has_character": False, "character_name": "", "franchise": ""})]

    async def scenario():
        return await TitleCharacterGateNode().post_async(
            shared, None, await TitleCharacterGateNode().exec_async(shared))

    action = asyncio.run(scenario())
    assert action == "no_good_news", action
    assert is_used("https://src/wolv"), "l'article sans perso doit être marqué used"
    log.info("PASSED")


def test_projet_franchise_sans_perso():
    shared = build_shared()
    shared["selected_article"] = {
        "title": "hololive Anime Project Announced with Studio KAI",
        "url": "https://src/holo", "source": "src",
        "score": 92, "hook_angle": "New hololive anime project",
    }
    _RESPONSES[:] = [json.dumps({"has_character": False, "character_name": "", "franchise": ""})]

    async def scenario():
        return await TitleCharacterGateNode().post_async(
            shared, None, await TitleCharacterGateNode().exec_async(shared))

    action = asyncio.run(scenario())
    assert action == "no_good_news", action
    assert is_used("https://src/holo")
    log.info("PASSED")


def test_cablage_title_gate():
    """Le graphe actufinder doit enchaîner ... -> title_gate -> validate_af_news.

    Scénario réel du bug : post_async retournait "approve", or seul l'edge
    "default" existait depuis title_gate -> le subflow s'arrêtait au gate,
    la validation Telegram n'était jamais envoyée et le pipeline sautait
    fetch/synthèse/extract pour aller directement au scriptwriter.
    """
    from nodes.actufinder import build_actufinder_flow

    flow = build_actufinder_flow()
    start = flow.start_node

    def _step_of(node):
        return (getattr(node, "step_name", None) or getattr(node, "step", None)
                or node.__class__.__name__)

    edges = []
    seen = set()

    def probe(node):
        if node in seen:
            return
        seen.add(node)
        for name, target in (getattr(node, "successors", {}) or {}).items():
            targets = target if isinstance(target, list) else [target]
            for t in targets:
                edges.append((_step_of(node), name, _step_of(t)))
                probe(t)

    probe(start)

    approve_gate = [(s, a, t) for s, a, t in edges
                    if s == "TitleCharacterGateNode" and a == "approve"]
    assert approve_gate, f"title_gate doit avoir un successeur 'approve': {edges}"
    assert "validate_af_news" in approve_gate[0][2], approve_gate
    nofeed_gate = [(s, a, t) for s, a, t in edges
                   if s == "TitleCharacterGateNode" and a == "no_good_news"]
    assert nofeed_gate and nofeed_gate[0][2] == "LLMSelectNode", nofeed_gate
    log.info("PASSED")


if __name__ == "__main__":
    test_perso_nomme()
    test_aucun_perso()
    test_projet_franchise_sans_perso()
    test_cablage_title_gate()
    clear_used()
    log.info("ALL PASSED")