"""Test "sujet seul" : même prompt simplifié + clause alone + anti-figurants.

Constat : le clip simplifié suit le sujet/décor/caméra, mais le modèle a
halluciné une 2e personne (maître tenant les jambes). La doc LTX-2.5 exige un
sujet unique explicite ("one subject, anyone else as blurred background").
SEULES différences vs clip_nosuffix (même seed 3439573003, mêmes réglages 8
steps cfg 3.0 guidance 3.5, 65f 704x1280) :
1. "She trains alone" dans le prompt.
2. "second person, two people, extra people, crowd" dans le négatif.

Usage : python3 tests/sdcpp_t2v_alone_test.py   (~12 min)
Sortie : downloads/alone_<date>/clip_alone.webm (+ run.log)
Verdict : à l'œil vs clip_nosuffix (le maître a-t-il disparu ? reste identique ?).
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
log = logging.getLogger("t2v-alone")

RUN_DIR = PIPELINE_ROOT / "downloads" / f"alone_{datetime.now():%Y%m%d_%H%M%S}"

PROMPT = (
    "She trains alone, dropping into a confident horse stance and holding it. "
    "A dark-haired martial artist in a blue qipao settles her weight as the fabric "
    "pulls tight across her thighs. "
    "A dramatic side light shapes her silhouette against a dark training hall. "
    "The camera tilts very slowly downward along her stance. "
    "Faint room tone, no music."
)

NEGATIVE = (
    "second person, two people, extra people, crowd, "
    "blurry face, deformed hands, text, watermark, indoor apartment, selfie, "
    "casual home video, static room, " + SDCPP_DEFAULT_NEGATIVE
)

SEED = 3439573003
STEPS = 8
FRAMES, WIDTH, HEIGHT = SDCPP_FRAMES, SDCPP_WIDTH, SDCPP_HEIGHT


def _env() -> dict:
    import os
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = SDCPP_ROCM_LIB
    env["GGML_CUDA_DISABLE_GRAPHS"] = "1"
    return env


def _build_cmd(output: Path) -> list[str]:
    return [
        SDCPP_BIN,
        "-M", "vid_gen",
        "--diffusion-model", SDCPP_LTX25_DIFFUSION,
        "--vae", SDCPP_LTX25_VAE,
        "--llm", SDCPP_LTX25_LLM,
        "--backend", "te=ROCm0,diffusion=ROCm0,vae=ROCm0",
        "--params-backend", "te=cpu,diffusion=ROCm0,vae=ROCm0",
        "--max-vram", "15",
        "--steps", str(STEPS),
        "--cfg-scale", "3.0",
        "--guidance", "3.5",
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


async def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    out = RUN_DIR / "clip_alone.webm"
    cmd = _build_cmd(out)
    with open(RUN_DIR / "run.log", "a") as f:
        f.write(f"seed={SEED} steps={STEPS} cfg=3.0 guidance=3.5 ALONE\n")
        f.write(f"prompt={PROMPT}\nnegative={NEGATIVE}\n")
    log.info(f"seed={SEED} ALONE -> {out}")

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
        log.error("TIMEOUT, killing")
        proc.terminate()
        await proc.wait()
        print("ALONE: ECHEC (timeout)")
        raise SystemExit(1)

    ret = await proc.wait()
    if ret != 0 or not out.is_file():
        log.error(f"exited {ret}\n" + buf.decode(errors="replace")[-4000:])
        print("ALONE: ECHEC")
        raise SystemExit(1)
    log.info(f"DONE -> {out} ({out.stat().st_size} o)")
    print(f"ALONE: OK -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
