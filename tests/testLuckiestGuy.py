import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from helpers.ffmpeg import write_ass, burn_subtitles, ffprobe
from config import SUBTITLE_FONTS_DIR

RUN_DIR = "downloads/test-assetfinder-short-1786706132687"
OUTPUT_DIR = "output/testLuckiestGuy"

os.makedirs(OUTPUT_DIR, exist_ok=True)


async def test():
    print("=== Test Luckiest Guy subtitle rendering ===\n")

    video_in = os.path.join(RUN_DIR, "clip_2.webm")
    video_out = os.path.join(OUTPUT_DIR, "subbed.mp4")
    ass_path = os.path.join(OUTPUT_DIR, "subtitles.ass")

    info = await ffprobe(video_in)
    duration = float(info.get("duration", 8.0))
    print(f"Input: {video_in} ({info.get('width')}x{info.get('height')}, {duration:.2f}s)")

    subtitles = [
        {"text": "Ce que j'aurais aimé savoir au début", "start_s": 0.0, "end_s": duration / 2},
        {"text": "Le secret c'est la régularité", "start_s": duration / 2, "end_s": duration},
    ]

    print(f"\nWriting ASS with Luckiest Guy style...")
    write_ass(subtitles, ass_path)
    with open(ass_path) as f:
        print(f.read())

    print(f"Burning subtitles with fontsdir={SUBTITLE_FONTS_DIR}...")
    await burn_subtitles(video_in, ass_path, video_out, fontsdir=SUBTITLE_FONTS_DIR)

    out_info = await ffprobe(video_out)
    print(f"Output: {video_out}")
    print(f"  Resolution: {out_info.get('width')}x{out_info.get('height')}")

    frame_out = os.path.join(OUTPUT_DIR, "frame.png")
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-ss", "1.0", "-i", video_out, "-frames:v", "1", frame_out], capture_output=True)
    print(f"  Frame: {frame_out}")

    assert os.path.exists(video_out), "Output video not created"
    assert "Luckiest Guy" in open(ass_path).read(), "Font not Luckiest Guy"
    print("\n✅ All checks passed!")


if __name__ == "__main__":
    asyncio.run(test())
