import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nodes.assetfinder.asset_planner import parse_script_assets
from nodes.assetfinder import build_assetfinder_flow

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


def check(cond: bool, label: str):
    icon = "✅" if cond else "❌"
    print(f"  {icon} {label}", flush=True)
    if not cond:
        sys.exit(1)


async def test():
    parsed = parse_script_assets(SCRIPT)
    print("=== PARSE ===", flush=True)
    print(f"  slots: {len(parsed['slots'])} (expected 2)", flush=True)
    print(f"  sfx: {len(parsed['sfx'])} (expected 1)", flush=True)
    print(f"  audio: {len(parsed['audio'])} (expected 1)", flush=True)
    print(f"  voiceover: {len(parsed['voiceover'])} (expected 2)", flush=True)
    check(len(parsed["slots"]) == 2, "2 visuals")
    check(len(parsed["sfx"]) == 1, "1 SFX")
    check(len(parsed["audio"]) == 1, "1 music")
    check(len(parsed["voiceover"]) == 2, "2 VO")

    flow = build_assetfinder_flow()

    shared = {
        "topic": "fitness motivation",
        "script": SCRIPT,
        "pipeline_id": f"test-assetfinder-short-{int(time.time() * 1000)}",
        "steps": [],
        "_traces": {},
    }

    print("\n=== FLOW ===", flush=True)
    await flow.run_async(shared)

    print("\n=== RESULTS ===", flush=True)
    for s in shared.get("steps", []):
        icon = {"ok": "✅", "error": "❌", "approve": "👍", "reformat": "🔁"}.get(s.get("status"), "⏳")
        out = str(s.get("output", ""))[:120].replace("\n", " ")
        print(f"  {icon} {s.get('step')}: {out}", flush=True)

    blueprint = shared.get("asset_blueprint", {})
    videos = shared.get("generated_videos", [])
    audio = shared.get("downloaded_audio", [])
    sfx = [a for a in audio if a.get("type") == "sfx"]
    music = [a for a in audio if a.get("type") == "music"]
    vo = [a for a in audio if a.get("type") == "voiceover"]

    print("\n=== ASSERTIONS ===", flush=True)
    check(len(blueprint.get("slots", [])) == 2, f"blueprint: 2 visual slots (got {len(blueprint.get('slots', []))})")
    check(len(videos) == 2, f"2 videos generated (got {len(videos)})")
    check(len(music) == 1, f"1 music generated (got {len(music)})")
    check(len(sfx) == 1, f"1 SFX generated (got {len(sfx)})")
    check(len(vo) == 2, f"2 VO generated (got {len(vo)})")
    check(not shared.get("_error"), f"no error (error={shared.get('_error')})")

    errors = [s for s in shared.get("steps", []) if s.get("status") == "error"]
    check(len(errors) == 0, f"0 steps in error (got {len(errors)})")

    print("\n=== FILES ===", flush=True)
    for vid in videos:
        print(f"  VID slot {vid.get('slot_id')}: {vid.get('video_path', 'none')} ({vid.get('duration_s', '?')}s)", flush=True)
    for a in audio:
        print(f"  AUD slot {a.get('slot_id')} [{a.get('type')}]: {a.get('path', 'none')}", flush=True)

    print("\nDone!", flush=True)


if __name__ == "__main__":
    asyncio.run(test())
