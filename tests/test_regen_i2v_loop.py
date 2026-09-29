#!/usr/bin/env python3
"""Test de la boucle Regen I2V (slot 2) — validate_af ⟷ image_gen ⟷ i2v_video.

Semi-automatisé : boutons résolus automatiquement, mais le PROMPT IMAGE est fourni
par réponse Telegram de l'utilisateur (MessageHandler → FeedbackInterpreterNode).

Déroulé :
  1. validate_af envoie les assets mock (2 clips + 4 audio) → auto : approve slot 1+audio,
     click 🎨 Regen I2V sur le slot 2.
  2. FeedbackInterpreterNode demande le prompt image → À TOI de répondre dans Telegram
     (ex: "un smoothie vert dans un verre, sur un fond de cuisine").
  3. i2v_cleanup (pkill sd-cli + comfyui_free) → ComfyUIImageGenerator (Klein 540×960).
  4. Validation image (phase 1) → auto-approve → i2v_video.
  5. i2v_video_cleanup (VRAM libérée) → SDCppI2VNode (build_i2v, 49f, GGML_CUDA_DISABLE_GRAPHS=1).
  6. Validation vidéo (phase 2) → auto-approve → done.

Usage:
  systemctl --user stop pocketflow-pipeline tg-callback-poller
  python3 tests/test_regen_i2v_loop.py
  systemctl --user start pocketflow-pipeline tg-callback-poller
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TG_BOT_TOKEN, TG_CHAT_ID
from helpers.state import _pending_validations, _resolve_validation, _resolve_validation_by_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("test-regen-i2v")

ASSET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "downloads", "test-af-full-1787494360352",
)

I2V_SLOT = 2
OTHER_SLOTS = [1, "s1", "a1", "v1", "v2"]
SCAN_INTERVAL = 0.25


def build_fake_shared():
    """Shared type run_af_validate_debug : vrais clips/audios, blueprint 2 slots."""
    return {
        "topic": "test regen i2v",
        "script": "Script de test pour la boucle Regen I2V.",
        "assets": "Hook: push-ups. Demo: smoothie.",
        "pipeline_id": f"test-i2v-{int(time.time() * 1000)}",
        "steps": [],
        "_traces": {},
        "asset_blueprint": {"slots": [
            {"id": 1, "prompt": "a fit person doing push-ups at sunrise",
             "content": "Hook", "section": "hook", "position": 0},
            {"id": 2, "prompt": "a person drinking a green smoothie",
             "content": "Demo smoothie", "section": "demo", "position": 1},
        ]},
        "generated_videos": [
            {"slot_id": 1, "video_path": os.path.join(ASSET_DIR, "clip_1.mp4"),
             "content": "Clip 1 (approuvé)", "duration_s": 4.06, "section": "hook"},
            {"slot_id": 2, "video_path": os.path.join(ASSET_DIR, "clip_2.mp4"),
             "content": "Clip 2 (rejeté → Regen I2V)", "duration_s": 4.06, "section": "demo"},
        ],
        "downloaded_audio": [
            {"slot_id": "s1", "type": "sfx", "path": os.path.join(ASSET_DIR, "sfx_s1.mp3"),
             "label": "SFX test", "query": "", "section": "sfx"},
            {"slot_id": "a1", "type": "music", "path": os.path.join(ASSET_DIR, "music_a1.mp3"),
             "mood": "test", "section": "music"},
            {"slot_id": "v1", "type": "voiceover", "path": os.path.join(ASSET_DIR, "vo_v1.mp3"),
             "text": "Voix test 1", "section": "vo"},
            {"slot_id": "v2", "type": "voiceover", "path": os.path.join(ASSET_DIR, "vo_v2.mp3"),
             "text": "Voix test 2", "section": "vo"},
        ],
    }


_stop = threading.Event()
_resolved = set()


def _resolve(key: str, result: str) -> None:
    if key in _resolved:
        return
    if _resolve_validation(key, result):
        _resolved.add(key)
        log.info(f"▶ resolved {key} <- {result}")


def watcher_loop(pid: str):
    """Scanne _pending_validations et résout par clé déterministe (slot 2 ciblé).

    Le prompt image (feedback) n'est PAS auto-résolu : il attend la réponse
    Telegram de l'utilisateur (gérée par tg_poller_loop).
    """
    while not _stop.is_set():
        for key in list(_pending_validations.keys()):
            entry = _pending_validations.get(key)
            if not entry or entry["event"].is_set():
                continue
            if key == f"{pid}_pf_af_{I2V_SLOT}":
                _resolve(key, f"i2v:{I2V_SLOT}")
            elif key == f"{pid}_pf_i2v_img_{I2V_SLOT}":
                _resolve(key, "approve")
            elif key == f"{pid}_pf_i2v_vid_{I2V_SLOT}":
                _resolve(key, "approve")
            elif any(key == f"{pid}_pf_af_{s}" for s in OTHER_SLOTS):
                _resolve(key, "approve")
        time.sleep(SCAN_INTERVAL)


def _tg_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"


def _answer_cb(callback_id: str) -> None:
    try:
        urllib.request.urlopen(
            _tg_url("answerCallbackQuery"),
            data=json.dumps({"callback_query_id": callback_id}).encode(),
            timeout=10,
        )
    except Exception:
        pass


def tg_poller_loop():
    """Mini-poller getUpdates : ne résout QUE les réponses feedback (messages reply).

    Uniquement utilisé pour capter la réponse de l'utilisateur au prompt image i2v.
    Les boutons sont auto-résolus par watcher_loop.
    """
    offset = None
    offset_file = "/tmp/tg_regen_i2v_offset.txt"
    try:
        with open(offset_file) as f:
            offset = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass

    while not _stop.is_set():
        try:
            params = {"timeout": 10, "allowed_updates": json.dumps(["callback_query", "message"])}
            if offset:
                params["offset"] = offset
            qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
            updates = json.loads(
                urllib.request.urlopen(f"{_tg_url('getUpdates')}?{qs}", timeout=15).read().decode()
            ).get("result", [])

            for upd in updates:
                if "callback_query" in upd:
                    cb = upd["callback_query"]
                    data = cb.get("data", "")
                    log.info(f"[tg] callback: {data[:80]} (clic manuel)")
                    _answer_cb(cb.get("id"))
                elif "message" in upd:
                    msg = upd["message"]
                    if str(msg.get("chat", {}).get("id", "")) != TG_CHAT_ID:
                        continue
                    text = msg.get("text", "")
                    reply_to = msg.get("reply_to_message", {}).get("message_id")
                    if text and reply_to:
                        ok = _resolve_validation_by_message(reply_to, text)
                        log.info(f"[tg] reply_to={reply_to} text={text[:60]}: {'ok' if ok else 'no match'}")
                        if not ok:
                            log.warning("Hmm, ta réponse n'a pas matché une validation (ok pour l'ignorer si elle venait d'une autre session)")
                    else:
                        log.info(f"[tg] msg sans reply: {text[:60]}")

                with open(offset_file, "w") as f:
                    f.write(str(upd["update_id"] + 1))
                offset = upd["update_id"] + 1

        except urllib.error.URLError:
            time.sleep(5)
        except Exception as e:
            log.warning(f"[tg] poller error: {e}")
            time.sleep(5)


def _probe(path: str, kind: str = "video") -> str:
    """ffprobe / identify − infos compactes, chaîne tolerant en cas d'échec."""
    try:
        if kind == "video":
            out = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,nb_frames,avg_frame_rate",
                 "-show_entries", "format=duration",
                 "-of", "default=nw=1", path],
                capture_output=True, text=True, timeout=20,
            ).stdout.strip().replace("\n", " | ")
            return out or "(aucune info)"
        out = subprocess.run(
            ["identify", "-format", "%wx%h %m", path],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return out or "(aucune info)"
    except FileNotFoundError:
        return "(ffprobe/identify absent)"
    except Exception as e:
        return f"(probe error: {e})"


async def test():
    if not TG_BOT_TOKEN:
        print("❌ PF_TG_BOT_TOKEN not set!")
        sys.exit(1)
    print(f"✅ TG token: {TG_BOT_TOKEN[:12]}...")

    shared = build_fake_shared()
    pid = shared["pipeline_id"]

    missing = []
    for v in shared["generated_videos"]:
        if not os.path.isfile(v["video_path"]):
            missing.append(v["video_path"])
    for a in shared["downloaded_audio"]:
        if not os.path.isfile(a["path"]):
            missing.append(a["path"])
    if missing:
        for m in missing:
            print(f"❌ Missing: {m}")
        sys.exit(1)
    print(f"✅ {len(shared['generated_videos'])} clips + {len(shared['downloaded_audio'])} audio trouvés")

    from main import build_af_validate_debug_flow

    watcher = threading.Thread(target=watcher_loop, args=(pid,), daemon=True)
    watcher.start()
    tg_poller = threading.Thread(target=tg_poller_loop, daemon=True)
    tg_poller.start()
    print(f"\n🚀 Boucle Regen I2V — slot {I2V_SLOT} ciblé ({pid})")
    print("   les boutons sont auto-résolus (slot 2 → i2v, reste → approve)")
    print("   ⚠️ RÉPONDS au message « 🎨 Régénération i2v… » avec TON prompt image")
    print("      (ex: « un smoothie vert dans un verre, sur un fond de cuisine »)")
    print("   Runtime estimé : ~2 min image Klein 540×960 + ~7-8 min i2v LTX\n")

    flow = build_af_validate_debug_flow()
    try:
        await flow.run_async(shared)
    finally:
        _stop.set()

    print("\n=== STEPS ===")
    for s in shared.get("steps", []):
        icon = {"ok": "✅", "error": "❌", "approve": "👍", "rework": "🔁", "reject": "🔁"}.get(s.get("status"), "⏳")
        out = str(s.get("output", ""))[:120].replace("\n", " ")
        print(f"  {icon} {s.get('step')}: {out}")

    if shared.get("_error"):
        print(f"\n❌ ERROR: {shared['_error']}")

    print("\n=== RÉSULTAT ===")
    print(f"pipeline_result: {shared.get('pipeline_result', '(n/a, debug flow)')}")

    print("\n-- generated_videos --")
    for v in shared.get("generated_videos", []):
        path = v.get("video_path", "")
        print(f"  slot {v.get('slot_id')}: {path}")
        print(f"    {_probe(path, 'video')}")
        print(f"    duration_s={v.get('duration_s')}")

    print("\n-- generated_images --")
    for g in shared.get("generated_images", []):
        path = g.get("image_path", "")
        print(f"  slot {g.get('slot_id')}: {path} (confirmed={g.get('confirmed')})")
        print(f"    {_probe(path, 'image')}")

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(test())