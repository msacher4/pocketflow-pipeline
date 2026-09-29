"""A/B cfg T2V LTX-2.5 — prompt EXACT du clip_3 raté (schoolgirls).

Le run debug-af-alt du 24/09 a produit des schoolgirls pour un prompt
"swordswoman sprinting" (seed 3733320982, cfg 3.0, guidance 3.5).
On tranche l'effet du cfg sur CE prompt-là, seed fixe partagé :
- Bras A = settings actuels prod (cfg 3.0 + guidance 3.5)
- Bras B = cfg 1.0 + guidance 3.5
- Bras C = cfg 1.0 SANS guidance (optionnel, arg C)

Usage:
  python3 tests/sdcpp_t2v_cfg_clip3.py A
  python3 tests/sdcpp_t2v_cfg_clip3.py B
  python3 tests/sdcpp_t2v_cfg_clip3.py C
Sorties: downloads/ab_cfg_clip3_<ts>/clip_{A,B,C}.webm (+ run.log)
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from config import (
    SDCPP_BIN, SDCPP_ROCM_LIB, SDCPP_LTX25_DIFFUSION, SDCPP_LTX25_VAE,
    SDCPP_LTX25_LLM, SDCPP_FRAMES, SDCPP_WIDTH, SDCPP_HEIGHT, SDCPP_TIMEOUT,
)
from helpers.sdcpp_api import SDCPP_DEFAULT_NEGATIVE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("t2v-cfg-clip3")

RUN_DIR = PIPELINE_ROOT / "downloads" / f"ab_cfg_clip3_{datetime.now():%Y%m%d_%H%M%S}"

# Prompt EXACT du slot 3 du run debug-af-alt-20260924_090303 (Video: script,
# après headcount alone_already -> aucun changement de prompt).
PROMPT = (
    "A dark armored swordswoman sprints alone through a shattered digital arena, "
    "her heavy black cape whipping behind her, debris flying with each stride, "
    "a slow tracking shot keeps her centered, metal heeled boots striking the "
    "ground, stark violet rays of light cutting through dust"
)

# Négatif exact assemblé prod pour ce slot (blueprint + default + anti-people).
NEGATIVE = (
    "blurry, low quality, text, watermark, deformed, indoor apartment, selfie, "
    "casual home video, static room, " + SDCPP_DEFAULT_NEGATIVE
    + ", second person, two people, extra people, crowd"
)

SEED = 3733320982  # seed du clip_3 raté -> comparaison directe
STEPS = 8
FRAMES, WIDTH, HEIGHT = SDCPP_FRAMES, SDCPP_WIDTH, SDCPP_HEIGHT

ARMS = {
    "A": {"cfg": "3.0", "guidance": True},
    "B": {"cfg": "1.0", "guidance": True},
    "C": {"cfg": "1.0", "guidance": False},
}


def _env() -> dict:
    import os
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = SDCPP_ROCM_LIB
    env["GGML_CUDA_DISABLE_GRAPHS"] = "1"
    return env


def _build_cmd(arm: str, output: Path) -> list[str]:
    cfg = ARMS[arm]
    cmd = [
        SDCPP_BIN,
        "-M", "vid_gen",
        "--diffusion-model", SDCPP_LTX25_DIFFUSION,
        "--vae", SDCPP_LTX25_VAE,
        "--llm", SDCPP_LTX25_LLM,
        "--backend", "te=ROCm0,diffusion=ROCm0,vae=ROCm0",
        "--params-backend", "te=cpu,diffusion=ROCm0,vae=ROCm0",
        "--max-vram", "15",
        "--steps", str(STEPS),
        "--cfg-scale", cfg["cfg"],
    ]
    if cfg["guidance"]:
        cmd += ["--guidance", "3.5"]
    cmd += [
        "--sampling-method", "euler",
        "-n", NEGATIVE,
        "--video-frames", str(FRAMES),
        "--width", str(WIDTH),
        "--height", str(HEIGHT),
        "--temporal-tiling",
        "--extra-tiling-args", "temporal_tile_frames=4",
        "--diffusion-fa",
        "--output", str(output),
        "-p", PROMPT,
        "-s", str(SEED),
        "-v",
    ]
    return cmd


async def run(arm: str) -> str | None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    out = RUN_DIR / f"clip_{arm}.webm"
    cmd = _build_cmd(arm, out)
    with open(RUN_DIR / "run.log", "a") as f:
        f.write(f"[{arm}] cfg={ARMS[arm]} seed={SEED} steps={STEPS}\n")
        f.write(f"prompt={PROMPT}\nnegative={NEGATIVE}\n")
    log.info(f"[{arm}] cfg={ARMS[arm]} seed={SEED} -> {out}")

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env()
    )
    buf = bytearray()
    try:
        async with asyncio.timeout(SDCPP_TIMEOUT):
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 200000:
                    buf = buf[-100000:]
    except TimeoutError:
        log.error(f"[{arm}] TIMEOUT, killing")
        proc.terminate()
        await proc.wait()
        return None

    ret = await proc.wait()
    if ret != 0 or not out.is_file():
        log.error(f"[{arm}] exited {ret}\n" + buf.decode(errors="replace")[-4000:])
        return None
    log.info(f"[{arm}] DONE -> {out} ({out.stat().st_size} o)")
    return str(out)


async def main():
    arm = sys.argv[1].upper() if len(sys.argv) > 1 else "B"
    if arm not in ARMS:
        print("Bras invalide (A, B ou C)")
        raise SystemExit(2)
    path = await run(arm)
    if not path:
        print(f"BRAS {arm}: ECHEC")
        raise SystemExit(1)
    print(f"BRAS {arm}: OK -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
