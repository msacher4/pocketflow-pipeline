"""Test minimal AssetFinder : 2 clips T2V + 2 voix off via Fish Speech S2 (profil woman_news).

Chaîne : InitCleanup -> CleanupLlamaProxy -> ComfyUIFreeMemory(stop) -> SDCppVideoGenerator
(T2V) -> ComfyUIFreeMemory(free) -> VoiceGenerator (s2). Les nodes de cleanup sont
CONSERVÉS (sinon OOM : VRAM partagée sd-cli / s2.cpp / llama-proxy).
Blueprint fourni directement : le mapping LLM AssetPlanner est déjà validé en prod.
Clips raccourcis (~2 s) pour aller vite.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ["PF_SDCPP_FRAMES"] = "33"  # ~2.06s @ 16fps au lieu de 4s
os.environ["PF_SDCPP_STEPS"] = "6"

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(PIPELINE_ROOT / "nodes"))

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from pocketflow import AsyncFlow
from nodes.assetfinder.init_cleanup import InitCleanup
from nodes.assetfinder.cleanup_llama_proxy import CleanupLlamaProxy
from nodes.assetfinder.comfyui_free_memory import ComfyUIFreeMemory
from nodes.assetfinder.sdcpp_video_generator import SDCppVideoGenerator
from nodes.assetfinder.voice_generator import VoiceGenerator

BLUEPRINT = {
    "slots": [
        {
            "id": 1, "section": "hook", "position": 0, "type": "visual",
            "content": "Warrior raising a glowing sword at dawn",
            "prompt": (
                "cinematic 9:16 portrait shot, a powerful warrior raising a glowing "
                "sword at dawn on a cliff, slow dolly-in camera, cape flowing in the "
                "wind, embers rising, dynamic action, never static"
            ),
            "negative_prompt": "blurry, low quality, text, watermark, static image",
        },
        {
            "id": 2, "section": "body", "position": 2, "type": "visual",
            "content": "Ruined city skyline under a violet storm",
            "prompt": (
                "cinematic 9:16 portrait shot, ruined city skyline under a violet "
                "storm, camera tracking forward, debris flying, heavy rain, neon "
                "lightning, particles in motion, never static"
            ),
            "negative_prompt": "blurry, low quality, text, watermark, static image",
        },
    ],
    "audio": [],
    "voiceover": [
        {"id": "v1", "section": "hook", "position": 1, "type": "voiceover",
         "text": "Welcome back, hunters.", "temperature": 0.7},
        {"id": "v2", "section": "body", "position": 3, "type": "voiceover",
         "text": "This update rewrites everything.", "temperature": 0.7},
    ],
    "sfx": [],
}


async def main():
    shared = {
        "topic": "fish speech s2 t2v voice test",
        "script": "",
        "asset_blueprint": BLUEPRINT,
        "pipeline_id": f"test-s2-fast-{int(time.time() * 1000)}",
        "steps": [],
        "_traces": {},
    }

    init = InitCleanup()
    cleanup = CleanupLlamaProxy()
    free_before_svg = ComfyUIFreeMemory(step="comfyui_free_svg", stop_service=True)
    svg = SDCppVideoGenerator()
    free_after_svg = ComfyUIFreeMemory(step="comfyui_free_after_svg", stop_service=False)
    vg = VoiceGenerator()
    init >> cleanup >> free_before_svg >> svg >> free_after_svg >> vg
    flow = AsyncFlow(start=init)
    await flow.run_async(shared)

    print("\n================ RESULTAT ================")
    for s in shared.get("steps", []):
        print(f"{s.get('step')}: {s.get('status')}")

    errors = [s for s in shared.get("steps", []) if s.get("status") != "ok"]
    if errors:
        print("\nERREURS:", json.dumps(errors, ensure_ascii=False)[:3000])
        raise SystemExit(1)

    ok = True

    videos = shared.get("generated_videos", [])
    print(f"\n{len(videos)} video(s) generee(s)")
    for v in videos:
        p = Path(v["video_path"])
        size = p.stat().st_size if p.is_file() else 0
        good = p.is_file() and size > 0
        print(f"  clip {v.get('slot_id')}: {v.get('video_path')} ({size} o) {'OK' if good else 'MANQUANT'}")
        ok = ok and good
    if len(videos) != 2:
        ok = False

    vos = [a for a in shared.get("downloaded_audio", []) if a.get("type") == "voiceover"]
    print(f"\n{len(vos)} voix off generee(s) via Fish Speech S2")
    for a in vos:
        p = Path(a["path"])
        if p.is_file():
            head = p.read_bytes()[:4]
            size = p.stat().st_size
        else:
            head, size = b"", 0
        good = p.is_file() and head == b"RIFF" and size > 0
        print(f"  {a.get('id')} [{a.get('section', '')}]: {a.get('path')} ({size} o, {head!r}) {'RIFF OK' if good else 'PAS DE WAV'}")
        ok = ok and good
    if len(vos) != 2:
        ok = False

    print("\nVOIX S2 + 2 T2V:", "OK" if ok else "ECHEC", f"({len(errors)} erreur(s))")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())