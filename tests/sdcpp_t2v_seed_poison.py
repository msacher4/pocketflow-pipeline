"""Test discriminant : prompt dragon (pourtant validé au T1) sur le seed POISON.

Résultats connus avant ce test :
- T1  dragon court + seed 42          -> OK
- T3  prompt clip_3 + seed 1234567    -> OK
- T3  prompt clip_3 + 5 autres seeds  -> slop
- L1  prompt clip_3 RACCOURCI + seed 3733320982 -> slop
- L2  prompt forme doc  + seed 3733320982       -> slop
- L3  cfg 5.0           + seed 3733320982       -> slop
- L4  steps 16          + seed 3733320982       -> TIMEOUT (900 s)

Question ici : le seed 3733320982 produit-il du slop pour N'IMPORTE QUEL
prompt (=> seed poison, la seule parade est multi-seeds + scoring), ou
seulement pour le sujet swordswoman/arena (=> le sujet compte) ?

Usage : python3 tests/sdcpp_t2v_seed_poison.py [5|6|all]   (~11 min/cas)
  T5 : dragon (7 tok)              + seed 3733320982 + neg minimal (comme T1)
  T6 : dragon (7 tok)              + seed 3733320982 + neg PROD (isole négatif)
Sortie : downloads/seed_poison/*.webm
"""

import asyncio
import logging
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from config import (
    SDCPP_BIN, SDCPP_ROCM_LIB, SDCPP_LTX25_DIFFUSION, SDCPP_LTX25_VAE,
    SDCPP_LTX25_LLM, SDCPP_FRAMES, SDCPP_WIDTH, SDCPP_HEIGHT, SDCPP_TIMEOUT,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed-poison")

OUT = PIPELINE_ROOT / "downloads" / "seed_poison"
OUT.mkdir(parents=True, exist_ok=True)

DRAGON = "A red dragon flying over a castle"

NEG_MIN = "blurry, low quality"

NEG_PROD = (
    "blurry, low quality, text, watermark, deformed, indoor apartment, selfie, "
    "casual home video, static room, worst quality, distorted, artifacts, "
    "second person, two people, extra people, crowd"
)

SEED = 3733320982

CASES = {
    "T5": dict(prompt=DRAGON, neg=NEG_MIN, note="dragon + neg minimal (identique T1)"),
    "T6": dict(prompt=DRAGON, neg=NEG_PROD, note="dragon + neg prod"),
}


def _env() -> dict:
    import os
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = SDCPP_ROCM_LIB
    env["GGML_CUDA_DISABLE_GRAPHS"] = "1"
    return env


def _build_cmd(case: dict, output: Path) -> list[str]:
    return [
        SDCPP_BIN, "-M", "vid_gen",
        "--diffusion-model", SDCPP_LTX25_DIFFUSION,
        "--vae", SDCPP_LTX25_VAE,
        "--llm", SDCPP_LTX25_LLM,
        "--backend", "te=ROCm0,diffusion=ROCm0,vae=ROCm0",
        "--params-backend", "te=cpu,diffusion=ROCm0,vae=ROCm0",
        "--max-vram", "15",
        "--steps", "8",
        "--cfg-scale", "3.0",
        "--guidance", "3.5",
        "--sampling-method", "euler",
        "-n", case["neg"],
        "--video-frames", str(SDCPP_FRAMES),
        "--width", str(SDCPP_WIDTH),
        "--height", str(SDCPP_HEIGHT),
        "--temporal-tiling",
        "--extra-tiling-args", "temporal_tile_frames=4",
        "--diffusion-fa",
        "--output", str(output),
        "-p", case["prompt"],
        "-s", str(SEED),
        "-v",
    ]


async def run_case(key: str, case: dict) -> bool:
    out = OUT / f"{key}.webm"
    with open(OUT / f"{key}.log", "w") as f:
        f.write(f"case={key} ({case['note']}) seed={SEED}\n")
        f.write(f"prompt={case['prompt']}\nnegative={case['neg']}\n")
    log.info(f"[{key}] {case['note']} seed={SEED} -> {out}")

    proc = await asyncio.create_subprocess_exec(
        *_build_cmd(case, out),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_env(),
    )
    buf = bytearray()
    try:
        async with asyncio.timeout(SDCPP_TIMEOUT):
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 300000:
                    buf = buf[-150000:]
    except TimeoutError:
        log.error(f"[{key}] TIMEOUT")
        proc.terminate()
        await proc.wait()
        return False

    ret = await proc.wait()
    with open(OUT / f"{key}.stdout.log", "w") as f:
        f.write(buf.decode(errors="replace"))
    if ret != 0 or not out.is_file():
        log.error(f"[{key}] exited {ret}")
        return False
    log.info(f"[{key}] DONE ({out.stat().st_size} bytes)")
    return True


async def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    keys = list(CASES) if which == "all" else [which]
    ok = 0
    for key in keys:
        if await run_case(key, CASES[key]):
            ok += 1
    print(f"DONE {ok}/{len(keys)}")
    raise SystemExit(0 if ok == len(keys) else 1)


if __name__ == "__main__":
    asyncio.run(main())
