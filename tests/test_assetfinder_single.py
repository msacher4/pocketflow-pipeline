import asyncio
import logging
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from nodes.assetfinder import build_assetfinder_flow


def check(cond: bool, label: str):
    icon = "✅" if cond else "❌"
    print(f"  {icon} {label}")
    if not cond:
        sys.exit(1)


async def test():
    flow = build_assetfinder_flow()

    shared = {
        "topic": "motivation fitness",
        "script": (
            "Hook: Tu peux tout faire.\n"
            "Body: Un seul geste : le squat.\n"
            "CTA: Like et abonne-toi."
        ),
        "pipeline_id": f"test-assetfinder-{int(time.time()*1000)}",
        "steps": [],
        "_traces": {},
    }

    print("Launching AssetFinder full flow (single-asset check)...")
    print(f"  topic: {shared['topic']}")
    print(f"  script: {shared['script']!r}")
    print()

    await flow.run_async(shared)

    print()
    print("=== STEPS ===")
    for s in shared.get("steps", []):
        icon = {"ok": "✅", "error": "❌", "approve": "👤", "reformat": "🔁"}.get(s.get("status"), "⏳")
        print(f"  {icon} {s.get('step')}: {s.get('output', '')[:100]}")

    print()
    print("=== ASSERTIONS (asset count non contraint) ===")
    blueprint = shared.get("asset_blueprint", {})
    slots = blueprint.get("slots", [])
    audio = blueprint.get("audio", [])
    videos = shared.get("generated_videos", [])

    check(len(slots) >= 1, f"blueprint: >=1 slot visuel (obtenu {len(slots)})")
    check(len(audio) >= 1, f"blueprint: >=1 slot audio (obtenu {len(audio)})")
    check(len(videos) >= 1, f"videos (upscalées): >=1 (obtenu {len(videos)})")
    check(bool(shared.get("assets", "").strip()), "montage approuvé (assets non vide)")
    check(not shared.get("_error"), f"pas d'erreur (error={shared.get('_error')})")

    step_keys = {s.get("step") for s in shared.get("steps", [])}
    for step in ("comfyui_free_ltx", "comfyui_upscale"):
        check(step in step_keys, f"step {step} présente dans le flux")

    errors = [s for s in shared.get("steps", []) if s.get("status") == "error"]
    check(len(errors) == 0, f"aucune step en error (obtenu {len(errors)})")

    print()
    for vid in videos:
        print(f"  VID slot {vid.get('slot_id')}: {vid.get('video_path', 'none')} ({vid.get('duration_s', '?')}s)")
    for a in shared.get("downloaded_audio", []):
        print(f"  AUD slot {a.get('slot_id')}: {a.get('path', 'none')}")

    print("\nDone! chaine complète validée.")


if __name__ == "__main__":
    asyncio.run(test())
