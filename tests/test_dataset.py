"""Tests du dataset d'entraînement (helpers/dataset.py + DatasetCollectorNode).

Aucun LLM/Telegram réel : on teste l'extraction VO, la construction de
l'enregistrement, l'écriture JSONL append-only, le skip auto-approve et la
capture par le node collector sur l'entrée 'approve'.
"""

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helpers.dataset as dsmod
from helpers.dataset import extract_vo_lines, build_record, record_validated_script
from helpers.state import set_auto_approve
from nodes.scriptwriter.dataset_collector import DatasetCollectorNode

logging.basicConfig(level=logging.WARNING)


SCRIPT = """Audio: upbeat JRPG anime
### HOOK (0-7s)
- Plan 1 (0-7s)
- Video: The dark queen rises
- VO: The queen is finally back.
### BODY (7-15s)
- Plan 2 (7-11s)
- Video: Swords clash over the kingdom
- VO: Swords clash everywhere now.
- Plan 3 (11-15s)
- Video: Arrows fly across the sky
VO: Arrows rain on the city.
### CTA (15-19s)
- Plan 4 (15-19s)
- Video: The queen crown glint
- VO: Follow for the next episode.
"""


def _shared(pipeline_id="pf_test", script=SCRIPT, source=None):
    s = {
        "pipeline_id": pipeline_id,
        "script": script,
        "topic": "Anime news",
        "_audio_mood_locked": "JRPG_BATTLE",
        "selected_article": {
            "title": "Big Anime News",
            "source": "Google News",
            "url": "https://example.com/a",
            "score": 9,
            "character_name": "Lilly",
            "franchise": "Magical Sisters",
            "hook_angle": "le retour",
            "synthesis": "Un long résumé de l'article.",
        },
        "thinking_agent": {"chosen_structure": "drama", "video_idea": "x"},
    }
    if source is not None:
        s["_script_source"] = source
    return s


def test_extract_vo_lines():
    vos = extract_vo_lines(SCRIPT)
    assert vos == [
        "The queen is finally back.",
        "Swords clash everywhere now.",
        "Arrows rain on the city.",
        "Follow for the next episode.",
    ], vos
    assert extract_vo_lines("") == []
    assert extract_vo_lines("Video: no VO here") == []


def test_build_record():
    rec = build_record(_shared(source="boost"), "boost")
    vos = [
        "The queen is finally back.",
        "Swords clash everywhere now.",
        "Arrows rain on the city.",
        "Follow for the next episode.",
    ]
    assert rec["vo_lines"] == vos, rec
    assert rec["script"] == "\n".join(vos), rec
    assert "Video:" not in rec["script"] and "Plan" not in rec["script"]
    assert rec["id"] == "pf_test"
    assert rec["source"] == "boost"
    assert rec["topic"] == "Anime news"
    assert rec["audio_mood"] == "JRPG_BATTLE"
    assert rec["article"]["title"] == "Big Anime News"
    assert rec["article"]["content"] == "Un long résumé de l'article."
    assert rec["thinking_agent"] == {"chosen_structure": "drama", "video_idea": "x"}
    assert json.loads(json.dumps(rec))  # sérialisable


def test_record_appends_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        dsmod.DATASET_DIR = Path(tmp)
        dsmod.DATASET_PATH = Path(tmp) / "sw_alt.jsonl"
        assert record_validated_script(_shared("pf_a"), "approve") is True
        assert record_validated_script(_shared("pf_b"), "boost") is True
        lines = dsmod.DATASET_PATH.read_text().strip().splitlines()
        assert len(lines) == 2, lines
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["id"] == "pf_a" and second["id"] == "pf_b"
        assert first["vo_lines"] == second["vo_lines"] == [
            "The queen is finally back.",
            "Swords clash everywhere now.",
            "Arrows rain on the city.",
            "Follow for the next episode.",
        ]
        assert first["script"] == "\n".join(first["vo_lines"])
        assert first["article"]["character_name"] == "Lilly"
        assert first["thinking_agent"]["chosen_structure"] == "drama"


def test_skip_empty_script():
    with tempfile.TemporaryDirectory() as tmp:
        dsmod.DATASET_DIR = Path(tmp)
        dsmod.DATASET_PATH = Path(tmp) / "sw_alt.jsonl"
        assert record_validated_script(_shared("pf_empty", script="  "), "approve") is False
        assert not dsmod.DATASET_PATH.exists()


def test_skip_auto_approve():
    with tempfile.TemporaryDirectory() as tmp:
        dsmod.DATASET_DIR = Path(tmp)
        dsmod.DATASET_PATH = Path(tmp) / "sw_alt.jsonl"
        set_auto_approve(True)
        try:
            assert record_validated_script(_shared("pf_auto"), "approve") is False
        finally:
            set_auto_approve(False)
        assert not dsmod.DATASET_PATH.exists()


def test_skip_debug_run():
    with tempfile.TemporaryDirectory() as tmp:
        dsmod.DATASET_DIR = Path(tmp)
        dsmod.DATASET_PATH = Path(tmp) / "sw_alt.jsonl"
        assert record_validated_script(_shared("debug-sw-alt-20260101_000000"), "approve") is False
        assert not dsmod.DATASET_PATH.exists()


def test_collector_node_writes_and_returns_approve():
    with tempfile.TemporaryDirectory() as tmp:
        dsmod.DATASET_DIR = Path(tmp)
        dsmod.DATASET_PATH = Path(tmp) / "sw_alt.jsonl"
        import nodes.scriptwriter.dataset_collector as dcmod
        dcmod.record_validated_script = dsmod.record_validated_script

        node = DatasetCollectorNode()
        shared = _shared("pf_node", source="feedback")

        async def run():
            prep = await node.prep_async(shared)
            action = await node.exec_async(prep)
            assert action == "approve"
            out = await node.post_async(shared, prep, action)
            assert out == "approve"

        asyncio.run(run())

        line = json.loads(dsmod.DATASET_PATH.read_text().strip())
        assert "The queen is finally back." in line["vo_lines"]
        assert len(line["vo_lines"]) == 4
        assert line["script"] == "\n".join(line["vo_lines"])
        assert line["source"] == "feedback"
        assert line["article"]["franchise"] == "Magical Sisters"


if __name__ == "__main__":
    test_extract_vo_lines()
    test_build_record()
    test_record_appends_jsonl()
    test_skip_empty_script()
    test_skip_auto_approve()
    test_skip_debug_run()
    test_collector_node_writes_and_returns_approve()
    print("dataset tests OK")
