"""Test des leviers d'adhésion T2V sur un seed FIXE connu mauvais.

Contexte (diagnostic terminé, voir tests/sdcpp_t2v_diag.py) :
- prompt clip_3 long + seed 3733320982 = slop dans 6 configurations
  (cfg 3.0 / cfg 1.0 / ±guidance / ±négatif lourd / prod), donc seed
  systématiquement "mauvais" -> banc d'essai stable.
- même prompt + seed 1234567 = bon clip -> le modèle SAIT suivre ce prompt.
- prompt dragon court + seed 42 = bon.

4 leviers testés ici, un seul variable à la fois, même seed 3733320982 :
  L1  prompt raccourci (~20 tok au lieu de 52)   -> effet longueur
  L2  prompt réécrit forme doc LTX-2.5           -> effet forme
  L3  cfg-scale 5.0 (au lieu de 3.0)             -> effet force CFG
  L4  steps 16 (au lieu de 8)                    -> effet pas

Usage : python3 tests/sdcpp_t2v_lever_test.py [L1|L2|L3|L4|all]   (~11 min/cas)
Sortie : downloads/lever_test/*.webm + *.log
Verdict : à l'œil — un levier "gagne" s'il transforme ce seed en clip conforme.
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
log = logging.getLogger("lever-test")

OUT = PIPELINE_ROOT / "downloads" / "lever_test"
OUT.mkdir(parents=True, exist_ok=True)

SEED = 3733320982  # seed systématiquement mauvais sur ce prompt

PROMPT_ORIG = (
    "A dark armored swordswoman sprints alone through a shattered digital arena, "
    "her heavy black cape whipping behind her, debris flying with each stride, "
    "a slow tracking shot keeps her centered, metal heeled boots striking the "
    "ground, stark violet rays of light cutting through dust"
)

PROMPT_SHORT = (
    "A dark armored swordswoman sprints through a shattered digital arena, "
    "her black cape whipping behind her, violet light cutting through dust"
)

PROMPT_DOC = (
    "A dark-armored swordswoman sprints across a shattered digital arena as "
    "debris bursts away from every stride. Her heavy black cape snaps and "
    "whips behind her while metal-heeled boots hammer the broken floor. "
    "Violet light rays cut through the drifting dust and flare each time she "
    "pushes off. The camera tracks along with her at chest height, keeping "
    "her centered while the ruined ground streaks past. Dust and sparks race "
    "her every step, filling the arena with motion."
)

NEG = (
    "blurry, low quality, text, watermark, deformed, indoor apartment, selfie, "
    "casual home video, static room, worst quality, distorted, artifacts, "
    "second person, two people, extra people, crowd"
)

CASES = {
    "L1": dict(prompt=PROMPT_SHORT, cfg="3.0", steps="8", note="prompt court"),
    "L2": dict(prompt=PROMPT_DOC, cfg="3.0", steps="8", note="forme doc"),
    "L3": dict(prompt=PROMPT_ORIG, cfg="5.0", steps="8", note="cfg 5.0"),
    "L4": dict(prompt=PROMPT_ORIG, cfg="3.0", steps="16", note="steps 16"),
}


def _env() -> dict:
    import os
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = SDCPP_ROCM_LIB
    env["GGML_CUDA_DISABLE_GRAPHS"] = "1"
    return env


def _build_cmd(case: dict, output: Path) -> list[str]:
    return [
        SDCPP_BIN,
        "-M", "vid_gen",
        "--diffusion-model", SDCPP_LTX25_DIFFUSION,
        "--vae", SDCPP_LTX25_VAE,
        "--llm", SDCPP_LTX25_LLM,
        "--backend", "te=ROCm0,diffusion=ROCm0,vae=ROCm0",
        "--params-backend", "te=cpu,diffusion=ROCm0,vae=ROCm0",
        "--max-vram", "15",
        "--steps", case["steps"],
        "--cfg-scale", case["cfg"],
        "--guidance", "3.5",
        "--sampling-method", "euler",
        "-n", NEG,
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
        f.write(f"case={key} ({case['note']}) seed={SEED} "
                f"cfg={case['cfg']} steps={case['steps']}\n")
        f.write(f"prompt={case['prompt']}\nnegative={NEG}\n")
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
