import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nodes.assetfinder import build_assetfinder_flow


async def test():
    flow = build_assetfinder_flow()

    shared = {
        "topic": "perte de poids",
        "script": (
            "Hook: Transforme ton corps en 30 jours avec ces 3 exercices simples.\n"
            "Body: 1. Squats - 3x15. 2. Push-ups - 3x10. 3. Planche - 3x30s.\n"
            "CTA: Like et follow pour plus de conseils fitness!"
        ),
        "pipeline_id": "test-assetfinder",
        "steps": [],
        "_traces": {},
    }

    print("Launching AssetFinder subflow (T2V + audio en parallèle)...")
    print(f"  topic: {shared['topic']}")
    print()

    await flow.run_async(shared)

    print()
    print("=== RESULTS ===")
    for s in shared.get("steps", []):
        icon = {"ok": "✅", "error": "❌", "approve": "👤"}.get(s.get("status"), "⏳")
        print(f"  {icon} {s.get('step')}: {s.get('output', '')[:100]}")

    videos = shared.get("generated_videos", [])
    print(f"\nGenerated videos: {len(videos)}")
    for vid in videos:
        print(f"  slot {vid.get('slot_id')}: {vid.get('video_path', 'none')}")

    audio = shared.get("downloaded_audio", [])
    print(f"Downloaded audio: {len(audio)}")

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(test())
