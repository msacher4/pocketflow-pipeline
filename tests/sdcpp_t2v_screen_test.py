"""Phase 1 — calibration du screening low-def : 4 seeds au verdict HD connu.

But : vérifier que le rendu BASSE DÉF croise les mêmes verdicts que le HD,
sur le prompt clip_3 (verdicts HD établis par tests/sdcpp_t2v_diag.py T3) :

  seed 1234567     -> HD ✅ bon      (swordswoman en arène violette)
  seed 3733320982  -> HD ❌ slop     (= clip_3 du run prod, byte-identique)
  seed 42          -> HD ❌ slop
  seed 987654321   -> HD ❌ slop

Params candidats screening (une seule variable vs HD : la définition) :
  352x640 (divisible par 32), 33 frames (33 ≡ 1 mod 8), 8 steps,
  cfg 3.0, guidance 3.5, euler, même prompt + même négatif prod.

Verdict : le low-def retrouve-t-il "1 bon / 3 slop" ?
  OUI -> corrélation validée, on peut construire le nœud seed_picker dessus.
  NON -> params de screening à ajuster (résolution plus proche du HD).

Usage : python3 tests/sdcpp_t2v_screen_test.py [seed|all]   (~3 min/cas)
Sortie : downloads/screen_test/*.webm + *.log
"""

import asyncio
import logging
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from config import (
    SDCPP_BIN, SDCPP_ROCM_LIB, SDCPP_LTX25_DIFFUSION, SDCPP_LTX25_VAE,
    SDCPP_LTX25_LLM, SDCPP_TIMEOUT,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("screen-test")

OUT = PIPELINE_ROOT / "downloads" / "screen_test"
OUT.mkdir(parents=True, exist_ok=True)

# Prompt exact du slot 3 (run debug-af-alt-20260924_090303) — identique au HD.
PROMPT_CLIP3 = (
    "A dark armored swordswoman sprints alone through a shattered digital arena, "
    "her heavy black cape whipping behind her, debris flying with each stride, "
    "a slow tracking shot keeps her centered, metal heeled boots striking the "
    "ground, stark violet rays of light cutting through dust"
)

# Négatif exact du run prod — identique au HD.
NEG_PROD = (
    "blurry, low quality, text, watermark, deformed, indoor apartment, selfie, "
    "casual home video, static room, worst quality, low quality, blurry, "
    "distorted, artifacts, second person, two people, extra people, crowd"
)

# Params screening (la SEULE différence vs HD est la définition).
SCREEN_WIDTH = 352
SCREEN_HEIGHT = 640
SCREEN_FRAMES = 33
SCREEN_STEPS = 8

# Verdict HD attendu par seed (documenté, pas deviné).
EXPECT = {
    1234567: "OK",
    3733320982: "SLOP",
    42: "SLOP",
    987654321: "SLOP",
}


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
        "--steps", str(SCREEN_STEPS),
        "--cfg-scale", "3.0",
        "--guidance", "3.5",
        "--sampling-method", "euler",
        "-n", negative,
        "--video-frames", str(SCREEN_FRAMES),
        "--width", str(SCREEN_WIDTH),
        "--height", str(SCREEN_HEIGHT),
        "--temporal-tiling",
        "--extra-tiling-args", "temporal_tile_frames=4",
        "--diffusion-fa",
        "--output", str(output),
        "-p", prompt,
        "-s", str(seed),
        "-v",
    ]


async def run_case(seed: int) -> bool:
    name = f"screen_seed{seed}"
    out = OUT / f"{name}.webm"
    with open(OUT / f"{name}.log", "w") as f:
        f.write(f"case={name} seed={seed} expect_hd={EXPECT.get(seed, '?')}\n")
        f.write(f"screen={SCREEN_WIDTH}x{SCREEN_HEIGHT} frames={SCREEN_FRAMES} "
                f"steps={SCREEN_STEPS} cfg=3.0 guidance=3.5\n")
        f.write(f"prompt={PROMPT_CLIP3}\nnegative={NEG_PROD}\n")
    log.info(f"[{name}] expect HD={EXPECT.get(seed, '?')} -> {out}")

    proc = await asyncio.create_subprocess_exec(
        *_build_cmd(PROMPT_CLIP3, NEG_PROD, seed, out),
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
        log.error(f"[{name}] TIMEOUT")
        proc.terminate()
        await proc.wait()
        return False

    ret = await proc.wait()
    with open(OUT / f"{name}.stdout.log", "w") as f:
        f.write(buf.decode(errors="replace"))
    if ret != 0 or not out.is_file():
        log.error(f"[{name}] exited {ret}")
        return False
    log.info(f"[{name}] DONE ({out.stat().st_size} bytes)")
    return True


async def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    seeds = list(EXPECT) if which == "all" else [int(which)]
    ok = 0
    for seed in seeds:
        if await run_case(seed):
            ok += 1
    print(f"DONE {ok}/{len(seeds)}")
    raise SystemExit(0 if ok == len(seeds) else 1)


if __name__ == "__main__":
    asyncio.run(main())
