import asyncio
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_DIR))

from helpers.sdcpp_api import sdcpp_generate_video_t2v

PROMPT = (
    "A long snaking queue of eager gamers outside a glowing demo booth seen "
    "from a fixed eye-level angle, slow gentle camera push-in, the crowd "
    "moving in place, shoulders shifting slowly, phone lights blinking, booth "
    "glow pulsing across waiting fans"
)


async def main() -> None:
    dest = PIPELINE_DIR / "tests" / "t2v_isolated"
    result = await sdcpp_generate_video_t2v(
        prompt=PROMPT,
        dest_dir=dest,
        name="gamer_queue_slowpush",
        seed=20260920,
        frames=65,
        width=704,
        height=1280,
        steps=8,
        timeout_s=900,
    )
    if result:
        print(f"OK: {result}")
        print(f"size: {Path(result).stat().st_size / 1048576:.1f} MB")
    else:
        print("FAILED: no output video")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())