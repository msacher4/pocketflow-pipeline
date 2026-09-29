#!/usr/bin/env python3
"""Test isolé du validate_af subflow avec de vrais fichiers.

Ne génère RIEN — réutilise les clips/audio d'un test précédent.
Teste : envoi TG → boutons → callback → FeedbackInterpreterNode → dash link.

Usage:
  systemctl --user stop pocketflow-pipeline tg-callback-poller
  python3 tests/test_assetfinder_tg.py
  → Vérifie Telegram, clique ✅ ou 🔄
  systemctl --user start pocketflow-pipeline tg-callback-poller
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
from helpers.state import _resolve_validation, _resolve_validation_by_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("test-af-tg")

DOWNLOADS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads")
ASSET_DIR = os.path.join(DOWNLOADS, "test-assetfinder-short-1786706132687")


def build_fake_shared():
    """Construit un shared avec les vrais fichiers d'un test précédent."""
    return {
        "topic": "fitness motivation",
        "script": (
            "### HOOK (0-3s)\n"
            "Audio: Upbeat motivational music\n"
            "Plan 1 (0-4s)\n"
            "Video: A fit person doing push-ups at sunrise\n"
            "VO: Here is the one exercise you need to start with.\n"
            "-- Transition --\n"
            "SFX: Whoosh\n"
            "### BODY (4-12s)\n"
            "Plan 2 (4-8s)\n"
            "Video: A person drinking a green smoothie\n"
            "VO: Consistency is the key to transformation.\n"
        ),
        "assets": "Hook: push-ups + smoothie. SFX: whoosh. Music: upbeat. VO: motivation.",
        "pipeline_id": f"test-af-tg-{int(time.time() * 1000)}",
        "steps": [],
        "_traces": {},
        "generated_videos": [
            {"slot_id": 1, "video_path": os.path.join(ASSET_DIR, "clip_1.webm"), "duration_s": 4.06,
             "content": "A fit person doing push-ups at sunrise"},
            {"slot_id": 2, "video_path": os.path.join(ASSET_DIR, "clip_2.webm"), "duration_s": 4.06,
             "content": "A person drinking a green smoothie"},
        ],
        "downloaded_audio": [
            {"slot_id": "s1", "type": "sfx", "path": os.path.join(ASSET_DIR, "sfx_s1.mp3"),
             "label": "Whoosh", "query": "whoosh transition"},
            {"slot_id": "a1", "type": "music", "path": os.path.join(ASSET_DIR, "music_a1.mp3"),
             "mood": "upbeat motivational"},
            {"slot_id": "v1", "type": "voiceover", "path": os.path.join(ASSET_DIR, "vo_v1.mp3"),
             "text": "Here is the one exercise you need to start with."},
            {"slot_id": "v2", "type": "voiceover", "path": os.path.join(ASSET_DIR, "vo_v2.mp3"),
             "text": "Consistency is the key to transformation."},
        ],
    }


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
    from nodes.validation import build_af_validation_flow

    if not TG_BOT_TOKEN:
        print("❌ PF_TG_BOT_TOKEN not set!")
        sys.exit(1)
    print(f"✅ TG token: {TG_BOT_TOKEN[:12]}...")

    shared = build_fake_shared()

    for v in shared["generated_videos"]:
        if not os.path.isfile(v["video_path"]):
            print(f"❌ Missing: {v['video_path']}")
            sys.exit(1)
    for a in shared["downloaded_audio"]:
        if not os.path.isfile(a["path"]):
            print(f"❌ Missing: {a['path']}")
            sys.exit(1)
    print(f"✅ {len(shared['generated_videos'])} videos, {len(shared['downloaded_audio'])} audio trouvés")

    flow = build_af_validation_flow()

    poller = threading.Thread(target=poller_loop, daemon=True)
    poller.start()
    print("✅ Mini-poller lancé")
    print("\n🚀 validate_af subflow — vérifie Telegram pour valider/rejeter !")
    print("   (timeout: 600s)\n")

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

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(test())
