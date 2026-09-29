"""Suite de diagnostic T2V LTX-2.5 — sans toucher à l'encoder.

Test 1 : prompt ultra-court (dragon) — isole encoder vs prompt long.
Test 2 : prompt clip_3 sans négatif — isole l'impact du négatif lourd.
Test 3 : prompt clip_3 × 6 seeds — isole seed-luck vs systémique.

Usage:
  python3 tests/sdcpp_t2v_diag.py 1
  python3 tests/sdcpp_t2v_diag.py 2
  python3 tests/sdcpp_t2v_diag.py 3
  python3 tests/sdcpp_t2v_diag.py all
"""

import asyncio
import logging
import random
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from config import (
    SDCPP_BIN, SDCPP_ROCM_LIB, SDCPP_LTX25_DIFFUSION, SDCPP_LTX25_VAE,
    SDCPP_LTX25_LLM, SDCPP_FRAMES, SDCPP_WIDTH, SDCPP_HEIGHT, SDCPP_TIMEOUT,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("t2v-diag")

OUT = PIPELINE_ROOT / "downloads" / "sanity_prompt_test"
OUT.mkdir(parents=True, exist_ok=True)

# Prompt exact du slot 3 (run debug-af-alt-20260924_090303)
PROMPT_CLIP3 = (
    "A dark armored swordswoman sprints alone through a shattered digital arena, "
    "her heavy black cape whipping behind her, debris flying with each stride, "
    "a slow tracking shot keeps her centered, metal heeled boots striking the "
    "ground, stark violet rays of light cutting through dust"
)

# Négatif exact du run prod (blueprint + default + anti-people)
NEG_PROD = (
    "blurry, low quality, text, watermark, deformed, indoor apartment, selfie, "
    "casual home video, static room, worst quality, low quality, blurry, "
    "distorted, artifacts, second person, two people, extra people, crowd"
)

SEEDS_3 = [42, 3733320982, 1234567, 987654321, 5555555, 246813579]


def _env() -> dict:
    import os
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = SDCPP_ROCM_LIB
    env["GGML_CUDA_DISABLE_GRAPHS"] = "1"
    return env


def _build_cmd(prompt: str, negative: str, seed: int, output: Path) -> list[str]:
    return [
        SDCPP_BIN,
        "-M", "vid_gen",
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
        "-n", negative,
        "--video-frames", str(SDCPP_FRAMES),
        "--width", str(SDCPP_WIDTH),
        "--height", str(SDCPP_HEIGHT),
        "--temporal-tiling",
        "--extra-tiling-args", "temporal_tile_frames=4",
        "--diffusion-fa",
        "--output", str(output),
        "-p", prompt,
        "-s", str(seed),
        "-v",
    ]


async def run_case(name: str, prompt: str, negative: str, seed: int) -> str | None:
    out = OUT / f"{name}.webm"
    cmd = _build_cmd(prompt, negative, seed, out)
    log_path = OUT / f"{name}.log"
    with open(log_path, "w") as f:
        f.write(f"case={name} seed={seed}\nprompt={prompt}\nnegative={negative}\n")
    log.info(f"[{name}] seed={seed} -> {out}")

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
                if len(buf) > 300000:
                    buf = buf[-150000:]
    except TimeoutError:
        log.error(f"[{name}] TIMEOUT")
        proc.terminate()
        await proc.wait()
        return None

    ret = await proc.wait()
    text = buf.decode(errors="replace")
    with open(OUT / f"{name}.stdout.log", "w") as f:
        f.write(text)
    if ret != 0 or not out.is_file():
        log.error(f"[{name}] exited {ret}")
        return None
    log.info(f"[{name}] DONE ({out.stat().st_size} bytes)")
    return str(out)


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    cases = []

    if which in ("1", "all"):
        cases.append((
            "t1_dragon_s42",
            "A red dragon flying over a castle",
            "blurry, low quality",
            42,
        ))
    if which in ("2", "all"):
        cases.append((
            "t2_clip3_noneg",
            PROMPT_CLIP3,
            "blurry, low quality",
            3733320982,
        ))
    if which in ("3", "all"):
        for s in SEEDS_3:
            cases.append((f"t3_clip3_seed{s}", PROMPT_CLIP3, NEG_PROD, s))

    if not cases:
        print("Usage: 1|2|3|all")
        raise SystemExit(2)

    ok = 0
    for name, prompt, neg, seed in cases:
        path = await run_case(name, prompt, neg, seed)
        if path:
            ok += 1
    print(f"DONE {ok}/{len(cases)}")
    raise SystemExit(0 if ok == len(cases) else 1)


if __name__ == "__main__":
    asyncio.run(main())
