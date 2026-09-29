import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from nodes.videoeditor import build_videoeditor_flow

RUN_DIR = "downloads/test-assetfinder-short-1786706132687"


def check(cond: bool, label: str):
    icon = "✅" if cond else "❌"
    print(f"  {icon} {label}")
    if not cond:
        sys.exit(1)


async def test():
    generated_videos = [
        {
            "slot_id": "1", "section": "Hook", "position": 1,
            "content": "Une femme fait des squats", "expected": "",
            "video_path": os.path.join(RUN_DIR, "clip_1.webm"),
            "duration_s": 4.0625,
        },
        {
            "slot_id": "2", "section": "Body", "position": 2,
            "content": "Une femme boit un smoothie", "expected": "",
            "video_path": os.path.join(RUN_DIR, "clip_2.webm"),
            "duration_s": 4.0625,
        },
    ]
    downloaded_audio = [
        {"slot_id": "s1", "type": "sfx", "query": "Whoosh", "path": os.path.join(RUN_DIR, "sfx_s1.mp3")},
        {"slot_id": "a1", "type": "music", "query": "Musique entraînante et motivante",
         "path": os.path.join(RUN_DIR, "music_a1.mp3")},
        {"slot_id": "v1", "type": "voiceover", "query": "Ce que j'aurais aimé savoir au début...",
         "path": os.path.join(RUN_DIR, "vo_v1.mp3")},
        {"slot_id": "v2", "type": "voiceover", "query": "Le secret, c'est la régularité.",
         "path": os.path.join(RUN_DIR, "vo_v2.mp3")},
    ]

    assets = (
        "## Plan de Montage — weightloss\n\n"
        "### Pistes Audio\n"
        f"1. {downloaded_audio[1]['path']} — musique motivante (fond)\n"
        f"2. {downloaded_audio[0]['path']} — whoosh (transition)\n"
        f"3. {downloaded_audio[2]['path']} — VO hook\n"
        f"4. {downloaded_audio[3]['path']} — VO body\n\n"
        "### Séquence\n"
        "0:00-0:04 Hook — " + os.path.join(RUN_DIR, "clip_1.webm") + " (4s) → musique de fond + VO hook\n"
        "0:04-0:08 Body — " + os.path.join(RUN_DIR, "clip_2.webm") + " (4s) → musique de fond + VO body\n"
    )

    shared = {
        "topic": "weightloss",
        "script": (
            "Ce que j'aurais aimé savoir au début...\n"
            "Le secret, c'est la régularité."
        ),
        "assets": assets,
        "generated_videos": generated_videos,
        "downloaded_audio": downloaded_audio,
        "pipeline_id": f"test-videoeditor-{int(time.time()*1000)}",
        "steps": [],
        "_traces": {},
    }

    print("Launching VideoEditor native flow (isolated)...")
    print(f"  pipeline_id: {shared['pipeline_id']}")
    print(f"  clips: {[v['video_path'] for v in generated_videos]}")
    print(f"  audio: {[a['path'] for a in downloaded_audio]}")
    print()

    flow = build_videoeditor_flow()
    await flow.run_async(shared)

    print()
    print("=== STEPS ===")
    for s in shared.get("steps", []):
        icon = {"ok": "✅", "error": "❌"}.get(s.get("status"), "⏳")
        print(f"  {icon} {s.get('step')}: {s.get('output', '')[:120]}")

    print()
    print("=== ASSERTIONS ===")
    check(not shared.get("_error"), f"pas d'erreur (error={shared.get('_error')})")

    final_path = shared.get("ve_final_path", "")
    check(bool(final_path) and os.path.exists(final_path), f"final.mp4 créé ({final_path})")

    info = shared.get("ve_final_info", {})
    check(info.get("duration_s", 0) >= 7.5, f"durée ≈ 8s (obtenue {info.get('duration_s')}s)")
    check(info.get("width", 0) >= 700 and info.get("height", 0) >= 1280, f"résolution verticale ({info.get('width')}x{info.get('height')})")

    check(bool(shared.get("video_result", "").strip()), "video_result (rapport markdown) non vide")

    errors = [s for s in shared.get("steps", []) if s.get("status") == "error"]
    check(len(errors) == 0, f"aucune step en error (obtenu {len(errors)})")

    print()
    print("=== VIDEO_RESULT ===")
    print(shared.get("video_result", ""))

    print("\nDone! VideoEditor validé avec les assets réels.")


if __name__ == "__main__":
    asyncio.run(test())