#!/usr/bin/env python3
"""Tests de la sélection LLM ActuFinder avec le critère personnage OBLIGATOIRE.

Vérifie que LLMSelectNode :
- rejette (no_good_news) une décision LLM sans character_name, même avec score 88 ;
- accepte une décision avec character_name et enrichit l'article ;
- exclut les URLs déjà marquées used (feed de ce run) de la liste proposée.

Usage:
  python3 tests/test_actufinder_selection.py
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nodes.actufinder.actufinder_node as amod
from nodes.actufinder.actufinder_node import LLMSelectNode
from nodes.actufinder.used_articles import clear_used, mark_used

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("test-af-selection")

_RESPONSES = []


async def fake_call_llm(model, system, user_msg, max_tokens=4096, timeout=180):
    return _RESPONSES.pop(0)


amod.call_llm = fake_call_llm


def _art(i, url):
    return {
        "title": f"news {i}", "url": url, "source": "src",
        "published_at": "Mon, 07 Sep 2026 10:00:00 GMT", "description": "desc",
    }


def build_shared():
    clear_used()
    return {
        "pipeline_id": "tselect",
        "steps": [],
        "_traces": {},
        "topic": "Test",
        "filtered_articles": [
            _art(1, "https://src/a1"), _art(2, "https://src/a2"), _art(3, "https://src/a3"),
        ],
    }


def test_rejet_sans_character_name():
    shared = build_shared()
    _RESPONSES[:] = [
        json.dumps({"selected_article": 1, "score": 88, "reason": "x", "hook_angle": "y"})
    ]

    async def scenario():
        return await LLMSelectNode().exec_async(shared)

    exec = asyncio.run(scenario())
    assert exec["status"] == "no_good_news", exec
    log.info("PASSED")


def test_accepte_avec_character_name():
    shared = build_shared()
    decision = {
        "selected_article": 2, "score": 91,
        "character_name": "Mai Shiranui", "franchise": "King of Fighters",
        "reason": "x", "hook_angle": "y",
    }
    _RESPONSES[:] = [json.dumps(decision)]

    async def scenario():
        return await LLMSelectNode().exec_async(shared)

    exec = asyncio.run(scenario())
    assert exec["status"] == "success", exec
    a = exec["article"]
    assert a["character_name"] == "Mai Shiranui"
    assert a["franchise"] == "King of Fighters"
    assert a["url"] == "https://src/a2"
    log.info("PASSED")


def test_exclut_urls_used():
    shared = build_shared()
    mark_used("https://src/a1")
    _RESPONSES[:] = [
        json.dumps({"selected_article": 1, "score": 90,
                    "character_name": "Claret", "franchise": "ZZZ",
                    "reason": "x", "hook_angle": "y"})
    ]

    async def scenario():
        return await LLMSelectNode().exec_async(shared)

    exec = asyncio.run(scenario())
    assert exec["status"] == "success", exec
    # a1 est exclu -> l'index 1 pointe maintenant sur a2
    assert exec["article"]["url"] == "https://src/a2", exec
    log.info("PASSED")


def test_score_trop_bas_rejete():
    shared = build_shared()
    _RESPONSES[:] = [
        json.dumps({"selected_article": 1, "score": 60,
                    "character_name": "X", "franchise": "Y",
                    "reason": "x", "hook_angle": "y"})
    ]

    async def scenario():
        return await LLMSelectNode().exec_async(shared)

    exec = asyncio.run(scenario())
    assert exec["status"] == "no_good_news", exec
    log.info("PASSED")


if __name__ == "__main__":
    test_rejet_sans_character_name()
    test_accepte_avec_character_name()
    test_exclut_urls_used()
    test_score_trop_bas_rejete()
    clear_used()
    log.info("ALL PASSED")