"""Test doc-prompt SIMPLIFIÉ (même sujet qipao/stance).

Constat : le test docprompt (6 phrases riches : poussière, souffle, sueur, vent)
a donné un thème arts martiaux mais vent abusé + détails perdus (kimono,
décor lycée). Hypothèse : trop de détails en compétition, surtout les mots
d'air/mouvement (bursts, drifting, breath, wind) que le modèle amplifie en
tempête, + décor non spécifié qu'il invente (lycée pourri).

Ce test : 5 phrases, action-first, 1 arc, décor EXPLICITE (salle d'entraînement
sombre — la doc exige un setting, le test précédent n'en avait pas), AUCUN mot
de vent/poussière/souffle/sueur, caméra/lumière/audio en clauses propres.
Même seed (3439573003), mêmes réglages (8 steps, cfg 3.0, guidance 3.5),
même négatif combiné, 65f 704x1280, SANS suffixe forcé.
Témoin = clip_doc (riche) : si le vent disparaît et qipao/stance tiennent,
c'est la surcharge de détails qui noyait le sujet.

Usage : python3 tests/sdcpp_t2v_nosuffix_test.py   (~12 min)
Sortie : downloads/nosuffix_<date>/clip_nosuffix.webm (+ run.log)
Verdict : à l'œil vs clip_doc (vent ? qipao ? stance ? décor ?).
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
log = logging.getLogger("t2v-nosuffix")

RUN_DIR = PIPELINE_ROOT / "downloads" / f"nosuffix_{datetime.now():%Y%m%d_%H%M%S}"

PROMPT = (
    "She drops into a confident horse stance and holds it. "
    "A dark-haired martial artist in a blue qipao settles her weight as the fabric "
    "pulls tight across her thighs. "
    "A dramatic side light shapes her silhouette against a dark training hall. "
    "The camera tilts very slowly downward along her stance. "
    "Faint room tone, no music."
)

NEGATIVE = (
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
    out = RUN_DIR / "clip_nosuffix.webm"
    cmd = _build_cmd(out)
    with open(RUN_DIR / "run.log", "a") as f:
        f.write(f"seed={SEED} steps={STEPS} cfg=3.0 guidance=3.5 SIMPLIFIÉ sans suffixe\n")
        f.write(f"prompt={PROMPT}\nnegative={NEGATIVE}\n")
    log.info(f"seed={SEED} SIMPLIFIÉ -> {out}")

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
        print("NOSUFFIX: ECHEC (timeout)")
        raise SystemExit(1)

    ret = await proc.wait()
    if ret != 0 or not out.is_file():
        log.error(f"exited {ret}\n" + buf.decode(errors="replace")[-4000:])
        print("NOSUFFIX: ECHEC")
        raise SystemExit(1)
    log.info(f"DONE -> {out} ({out.stat().st_size} o)")
    print(f"NOSUFFIX: OK -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
