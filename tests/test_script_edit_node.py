"""Tests de l'édition directe du script (ScriptEditFeedbackNode) et du fix
no-slot de FeedbackInterpreterNode.

Scénarios couverts (aucun LLM réel, aucun Telegram réel) :
- édition partielle (lignes 'Plan N VO:' / 'Plan N Video:') fusionnée + retimée
- remplacement complet (script collé)
- FeedbackInterpreterNode pose la question même sans _rejected_slot
  (fix du "Rejeter avec feedback" qui régénérait en silence)
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nodes.validation.feedback_node as fbmod
from nodes.validation.feedback_node import (
    FeedbackInterpreterNode,
    ScriptEditFeedbackNode,
    ScriptBoostNode,
)
from helpers.state import _pending_validations
from helpers.call_llm import _extract_json

logging.basicConfig(level=logging.WARNING)


SCRIPT = """Audio: upbeat JRPG anime
### HOOK (0-7s)
- Plan 1 (0-7s)
- Video: The dark queen rises
- VO: The queen is finally back.
### BODY (7-11s)
- Plan 2 (7-10s)
- Video: Swords clash over the kingdom
- VO: Swords clash everywhere now.
- Plan 3 (10-14s)
- Video: Arrows fly across the sky
- VO: Arrows rain on the city.
### CTA (14-18s)
- Plan 4 (14-17s)
- Video: The queen crown glint
- VO: Follow for the next episode.
"""

SENT = []
MSG_ID = 1234
RESOLVED_TEXT = ""


async def fake_send_telegram(text, buttons=None):
    SENT.append({"text": text, "buttons": buttons})
    return MSG_ID


fbmod.send_telegram = fake_send_telegram
fbmod.TG_BOT_TOKEN = "test-token"


def _run(node, shared):
    async def inner():
        await node.prep_async(shared)
        exec = await node.exec_async(shared)
        action = await node.post_async(shared, None, exec)
        data = json.loads(exec) if isinstance(exec, str) else exec
        return data, action

    return inner


def _build_shared():
    return {
        "pipeline_id": "t_edit",
        "step_name": "validate_sw_alt",
        "steps": [],
        "_traces": {},
        "script": SCRIPT,
        "selected_article": {"title": "Ruan Mei news"},
        "topic": "actu",
    }


async def _wait_registered_and_resolve(text, timeout=4.0):
    """Poll jusqu'à ce que le node ait enregistré sa validation Teleegram
    (le node fait des I/O en prep), puis résout la réponse de l'utilisateur."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for vid, entry in list(_pending_validations.items()):
            if MSG_ID in entry.get("message_ids", []):
                entry["result"] = f"feedback:{text}"
                entry["event"].set()
                return True
        await asyncio.sleep(0.01)
    return False


def test_edit_partiel():
    shared = _build_shared()
    node = ScriptEditFeedbackNode(step_name="validate_sw_alt")
    edited = "Plan 3 VO: A new much better closing line.\nPlan 1 Video: Close-up golden eyes."

    async def scenario():
        task = asyncio.create_task(_run(node, shared)())
        ok = await _wait_registered_and_resolve(edited)
        assert ok, "validation jamais enregistrée"
        return await task

    data, action = asyncio.run(scenario())
    assert action == "edit", action
    script = data["script"]
    assert "A new much better closing line." in script
    assert "Close-up golden eyes." in script, script
    # l'ancienne phrase de fermeture du plan 3 a disparu
    plan3 = script.split("Plan 3")[1]
    assert "Arrows rain on the city." not in plan3
    # retiming : plan 1 (I2V ≈4s) redeclaré 0-4s
    assert "Plan 1 (0-4s)" in script, script
    # tout le script est toujours parseable en plans
    from nodes.scriptwriter.script_timing import parse_plans
    plans = parse_plans(script)
    assert len(plans) == 4, len(plans)
    _pending_validations.clear()
    print("test_edit_partiel PASSED")


def test_edit_script_complet():
    shared = _build_shared()
    node = ScriptEditFeedbackNode(step_name="validate_sw_alt")
    full = """### HOOK (0-4s)
- Plan 1 (0-3s)
- Video: Replacement visual
- VO: Totally new script line.
"""

    async def scenario():
        task = asyncio.create_task(_run(node, shared)())
        ok = await _wait_registered_and_resolve(full)
        assert ok
        return await task

    data, action = asyncio.run(scenario())
    assert action == "edit"
    assert "Totally new script line." in data["script"]
    _pending_validations.clear()
    print("test_edit_script_complet PASSED")


def test_edit_timeout_sans_changement():
    shared = _build_shared()
    node = ScriptEditFeedbackNode(step_name="validate_sw_alt")
    node.timeout = 0.05

    async def scenario():
        return await _run(node, shared)()

    data, action = asyncio.run(scenario())
    assert action == "edit"
    assert data["script"] == SCRIPT
    _pending_validations.clear()
    print("test_edit_timeout_sans_changement PASSED")


FAKE_INTERPRET = {
    "decision": "rework",
    "target": "scriptwriter_alt",
    "instructions": "Tens un peu plus le rythme et resserre le hook.",
}


async def fake_call_llm(model, soul, ctx):
    return json.dumps(FAKE_INTERPRET)


def test_feedback_no_slot_question():
    """Sans slot rejeté (script entier rejeté), le feedback doit POSER la
    question et interpréter la réponse — plus jamais régénérer en silence."""
    shared = _build_shared()
    fbmod.call_llm = fake_call_llm
    fbmod._trace_llm = lambda *a, **k: None
    node = FeedbackInterpreterNode(
        step_name="validate_sw_alt",
        format_proposal=lambda s: "Script proposal",
        default_target="scriptwriter_alt",
        allowed_targets=["scriptwriter_alt"],
    )
    shared["_rejected_slot"] = None

    async def scenario():
        task = asyncio.create_task(_run(node, shared)())
        ok = await _wait_registered_and_resolve("c'est trop plat, accélère le hook")
        assert ok
        return await task

    data, action = asyncio.run(scenario())
    assert data.get("decision") == "rework", data
    assert data.get("target") == "scriptwriter_alt"
    assert data.get("instructions")
    assert data.get("raw_text") == "c'est trop plat, accélère le hook"
    assert action == "scriptwriter_alt"
    _pending_validations.clear()
    print("test_feedback_no_slot_question PASSED")


# ---------------------------------------------------------------------------
# Boost (⚡) — bypass total des validations
# ---------------------------------------------------------------------------


IMPESED = """Audio: UPBEAT_GAMING
### HOOK (0-4s)
- Plan 1 (0-4s)
- Video: Kurumi slashes the arena gates wide open.
- VO: Kurumi Fukuga breaks the pillars.
"""


def test_boost_imposes_script_strict():
    """Le script collé remplace `script` EN L'ÉTAT (aucune revalidation, aucun
    retime : les horaires déclarés par l'utilisateur sont conservés)."""
    shared = _build_shared()
    node = ScriptBoostNode(step_name="validate_sw_alt_boost")

    async def scenario():
        task = asyncio.create_task(_run(node, shared)())
        ok = await _wait_registered_and_resolve(IMPESED)
        assert ok
        return await task

    data, action = asyncio.run(scenario())
    assert action == "approve", action
    assert data["script"] == IMPESED.strip(), repr(data["script"])
    # post_async écrit bien le script imposé dans shared
    assert shared["script"] == IMPESED.strip()
    _pending_validations.clear()
    print("test_boost_imposes_script_strict PASSED")


def test_boost_timeout_cancel():
    """Timeout sans réponse → action 'cancel', script inchangé (on revient à
    la validation, jamais d'enchaînement)."""
    shared = _build_shared()
    node = ScriptBoostNode(step_name="validate_sw_alt_boost")
    node.timeout = 0.05

    async def scenario():
        return await _run(node, shared)()

    data, action = asyncio.run(scenario())
    assert action == "cancel", action
    assert data["script"] == SCRIPT
    assert shared["script"] == SCRIPT
    _pending_validations.clear()
    print("test_boost_timeout_cancel PASSED")


def test_boost_empty_reply_cancel():
    """Réponse vide → 'cancel' (pas de script à imposer, pas d'approve)."""
    shared = _build_shared()
    node = ScriptBoostNode(step_name="validate_sw_alt_boost")

    async def scenario():
        task = asyncio.create_task(_run(node, shared)())
        ok = await _wait_registered_and_resolve("   ")
        assert ok
        return await task

    data, action = asyncio.run(scenario())
    assert action == "cancel", action
    assert shared["script"] == SCRIPT
    _pending_validations.clear()
    print("test_boost_empty_reply_cancel PASSED")


if __name__ == "__main__":
    test_edit_partiel()
    test_edit_script_complet()
    test_edit_timeout_sans_changement()
    test_feedback_no_slot_question()
    test_boost_imposes_script_strict()
    test_boost_timeout_cancel()
    test_boost_empty_reply_cancel()
    print("ALL PASSED")