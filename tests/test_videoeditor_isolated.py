import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nodes.videoeditor import build_videoeditor_flow

RUN_DIR = "downloads/test-assetfinder-short-1786987435533"


def check(cond: bool, label: str):
    icon = "✅" if cond else "❌"
    print(f"  {icon} {label}", flush=True)
    if not cond:
        sys.exit(1)


async def test():
    print("=== VideoEditor isolated test ===\n", flush=True)

    clip1 = os.path.abspath(f"{RUN_DIR}/clip_1.mp4")
    clip2 = os.path.abspath(f"{RUN_DIR}/clip_2.mp4")
    music = os.path.abspath(f"{RUN_DIR}/music_a1.mp3")
    sfx = os.path.abspath(f"{RUN_DIR}/sfx_s1.mp3")
    vo1 = os.path.abspath(f"{RUN_DIR}/vo_v1.mp3")
    vo2 = os.path.abspath(f"{RUN_DIR}/vo_v2.mp3")

    for f in [clip1, clip2, music, sfx, vo1, vo2]:
        assert os.path.exists(f), f"Missing: {f}"

    shared = {
        "topic": "fitness motivation",
        "script": "### HOOK\nAudio: Upbeat motivational music\nPlan 1\nVideo: A fit person doing push-ups at sunrise\nVO: Here is the one exercise you need to start with.\nSFX: Whoosh\n### BODY\nPlan 2\nVideo: A person drinking a green smoothie\nVO: Consistency is the key to transformation.",
        "pipeline_id": f"test-ve-{int(time.time() * 1000)}",
        "steps": [],
        "_traces": {},
        "generated_videos": [
            {
                "slot_id": 1,
                "section": "hook",
                "position": 1,
                "content": "A fit person doing push-ups at sunrise",
                "expected": "A fit person doing push-ups at sunrise",
                "video_path": clip1,
                "duration_s": 4.30,
            },
            {
                "slot_id": 2,
                "section": "body",
                "position": 2,
                "content": "A person drinking a green smoothie",
                "expected": "A person drinking a green smoothie",
                "video_path": clip2,
                "duration_s": 4.30,
            },
        ],
        "downloaded_audio": [
            {
                "slot_id": "s1",
                "type": "sfx",
                "query": "Whoosh",
                "path": sfx,
                "section": "hook",
                "position": 2,
            },
            {
                "slot_id": "a1",
                "type": "music",
                "query": "Upbeat motivational music",
                "path": music,
                "section": "hook",
                "position": 0,
            },
            {
                "slot_id": "v1",
                "type": "voiceover",
                "text": "Here is the one exercise you need to start with.",
                "path": vo1,
                "section": "hook",
                "position": 1,
            },
            {
                "slot_id": "v2",
                "type": "voiceover",
                "text": "Consistency is the key to transformation.",
                "path": vo2,
                "section": "body",
                "position": 3,
            },
        ],
        "assets": "## Montage Plan\n\n### Hook (0-4s)\n1. clip_1.mp4 — push-ups at sunrise\n   - VO: Here is the one exercise you need to start with.\n   - SFX: Whoosh\n\n### Body (4-8s)\n2. clip_2.mp4 — green smoothie\n   - VO: Consistency is the key to transformation.",
    }

    flow = build_videoeditor_flow()

    print("=== FLOW ===", flush=True)
    await flow.run_async(shared)

    print("\n=== RESULTS ===", flush=True)
    for s in shared.get("steps", []):
        icon = {"ok": "✅", "error": "❌"}.get(s.get("status"), "⏳")
        out = str(s.get("output", ""))[:150].replace("\n", " ")
        print(f"  {icon} {s.get('step')}: {out}", flush=True)

    final = shared.get("ve_final_path", "")
    print(f"\n=== FINAL VIDEO ===", flush=True)
    print(f"  Path: {final}")
    check(final and os.path.exists(final), f"final video exists: {final}")

    # Check subtitle files
    project_dir = shared.get("ve_project_dir", "")
    ass_path = os.path.join(project_dir, "subtitles.ass") if project_dir else ""
    if ass_path and os.path.exists(ass_path):
        with open(ass_path) as f:
            content = f.read()
        n_dialogues = content.count("Dialogue:")
        print(f"  ASS: {ass_path} ({n_dialogues} dialogue lines)")
    else:
        print(f"  ASS: not found (whisper may have skipped)")

    check(not shared.get("_error"), f"no error (error={shared.get('_error')})")

    errors = [s for s in shared.get("steps", []) if s.get("status") == "error"]
    check(len(errors) == 0, f"0 steps in error (got {len(errors)})")

    print("\nDone!", flush=True)


if __name__ == "__main__":
    asyncio.run(test())
