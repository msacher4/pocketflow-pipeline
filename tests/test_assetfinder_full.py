#!/usr/bin/env python3
"""Test complet du pipeline AssetFinder — flow entier avec vrai script.

Étapes:
  1. Stop daemon + poller
  2. Lance le flow complet (AssetPlanner → generators → validate → feedback → regen → ...)
  3. Vérifie Telegram pour valider/rejeter les assets

Usage:
  python3 tests/test_assetfinder_full.py
"""
import asyncio
import json
import logging
import os
import sys
import time
import threading
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TG_BOT_TOKEN, TG_CHAT_ID
from helpers.state import (
    _resolve_validation, _resolve_validation_by_message,
    _set_state, _shared_snapshot, _set_traces,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("test-af-full")

SCRIPT = """### HOOK (0-3s)
Audio: Upbeat motivational music

Plan 1 (0-4s)
Video: A fit person doing push-ups at sunrise
VO: Here is the one exercise you need to start with.

-- Transition --
SFX: Whoosh

### BODY (4-12s)
Plan 2 (4-8s)
Video: A person drinking a green smoothie
VO: Consistency is the key to transformation.
"""

_stop_poller = threading.Event()


def poller_loop():
    """Mini-poller qui resolve les validations dans le même process."""
    offset = None
    offset_file = "/tmp/tg_poller_test_offset.txt"
    try:
        with open(offset_file) as f:
            offset = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass

    while not _stop_poller.is_set():
        try:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates"
            params = {"timeout": 10, "allowed_updates": json.dumps(["callback_query", "message"])}
            if offset:
                params["offset"] = offset
            qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
            resp = urllib.request.urlopen(f"{url}?{qs}", timeout=15)
            updates = json.loads(resp.read().decode()).get("result", [])

            for upd in updates:
                if "callback_query" in upd:
                    cb = upd["callback_query"]
                    data = cb.get("data", "")
                    cb_id = cb.get("id")

                    if ":" not in data:
                        _answer_cb(cb_id)
                        continue

                    action, rest = data.split(":", 1)
                    parts = rest.split(":")

                    if action in ("approve", "reject"):
                        vid = parts[0]
                        slot_id = parts[1] if len(parts) > 1 else ""
                        if slot_id:
                            ok = _resolve_validation(f"{vid}_{slot_id}", action)
                        else:
                            ok = _resolve_validation(vid, action)
                        log.info(f"CB {action} vid={vid} slot={slot_id}: {'resolved' if ok else 'NOT FOUND'}")
                    elif action == "img_approve":
                        vid = parts[0] if parts else ""
                        slot_id = parts[1] if len(parts) > 1 else ""
                        ok = _resolve_validation(vid, f"approve:{slot_id}")
                        log.info(f"CB img_approve vid={vid} slot={slot_id}: {'resolved' if ok else 'NOT FOUND'}")
                    elif action == "img_approve_all":
                        vid = parts[0] if parts else ""
                        ok = _resolve_validation(vid, "approve_all")
                        log.info(f"CB img_approve_all vid={vid}: {'resolved' if ok else 'NOT FOUND'}")
                    elif action == "img_reject_all":
                        vid = parts[0] if parts else ""
                        ok = _resolve_validation(vid, "reject_all")
                        log.info(f"CB img_reject_all vid={vid}: {'resolved' if ok else 'NOT FOUND'}")

                    _answer_cb(cb_id)

                elif "message" in upd:
                    msg = upd["message"]
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != TG_CHAT_ID:
                        continue
                    text = msg.get("text", "")
                    message_id = msg.get("message_id")
                    reply_to = msg.get("reply_to_message", {}).get("message_id")
                    if text and reply_to:
                        ok = _resolve_validation_by_message(reply_to, text)
                        log.info(f"MSG reply_to={reply_to} text={text[:50]}: {'resolved' if ok else 'no match'}")

                new_offset = upd["update_id"] + 1
                with open(offset_file, "w") as f:
                    f.write(str(new_offset))
                offset = new_offset

        except urllib.error.URLError:
            time.sleep(5)
        except Exception as e:
            log.warning(f"Poller error: {e}")
            time.sleep(5)


def _answer_cb(callback_id):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery"
    data = json.dumps({"callback_query_id": callback_id}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception:
        pass


async def test():
    from nodes.assetfinder import build_assetfinder_flow

    if not TG_BOT_TOKEN:
        print("❌ PF_TG_BOT_TOKEN not set!")
        sys.exit(1)
    print(f"✅ TG token: {TG_BOT_TOKEN[:12]}...")

    pipeline_id = f"test-af-full-{int(time.time() * 1000)}"

    shared = {
        "topic": "fitness motivation",
        "script": SCRIPT,
        "pipeline_id": pipeline_id,
        "steps": [],
        "_traces": {},
    }

    await _set_state(**_shared_snapshot(shared))

    flow = build_assetfinder_flow()

    poller = threading.Thread(target=poller_loop, daemon=True)
    poller.start()
    print("✅ Mini-poller lancé")
    print(f"\n🚀 Pipeline AssetFinder complet — pipeline_id: {pipeline_id}")
    print("   Le flow va: AssetPlanner → generators → MontagePlanner → ValidateImages")
    print("   → Feedback → CombinedCleanup → generator → re-validate → ...")
    print("   Vérifie Telegram pour voir les assets et cliquer ✅/🔄\n")

    await flow.run_async(shared)

    _stop_poller.set()

    print("\n=== STEPS ===")
    for s in shared.get("steps", []):
        icon = {"ok": "✅", "error": "❌", "approve": "👍", "reformat": "🔁", "rework": "🔁"}.get(s.get("status"), "⏳")
        out = str(s.get("output", ""))[:120].replace("\n", " ")
        target = s.get("target", "")
        target_str = f" → {target}" if target else ""
        print(f"  {icon} {s.get('step')}: {out}{target_str}")

    feedback = shared.get("af_feedback", "")
    if feedback:
        print(f"\n📝 Feedback reçu: {feedback[:300]}")
    target = shared.get("user_feedback", {}).get("target", "")
    if target:
        print(f"🎯 Target choisi: {target}")
    instructions = shared.get("user_feedback", {}).get("instructions", "")
    if instructions:
        print(f"📋 Instructions: {instructions[:300]}")

    print("\n✅ Test terminé!")


if __name__ == "__main__":
    asyncio.run(test())
