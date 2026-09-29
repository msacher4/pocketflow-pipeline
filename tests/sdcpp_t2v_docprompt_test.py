"""Test prompt format DOC OFFICIELLE LTX-2.5 (même sujet que le clip 4 raté).

Règles appliquées (ltx.io/blog + docs.ltx.io) :
- UN paragraphe fluide, présent, 4-8 phrases — PAS de minutage [0-1s], PAS de
  shot-list (formats explicitement "ignorés" par la doc).
- OUVRIR par l'ACTION ("leading with a subject noun phrase -> near-static clip").
- UNE action dominante (two arcs competing lose fidelity).
- Mouvement caméra dans sa clause, relatif au sujet.
- UNE logique de lumière cohérente.
- Émotion/état par indices physiques, pas de labels.
- Clause audio courte (le modèle génère le son conjointement).
- PAS de bourrage keywords ("cinematic", "masterpiece", "8K" = sans effet).

Technique FIGÉE sur la prod actuelle (8 steps, cfg-scale 3.0, guidance 3.5),
même seed que le clip 4 (3439573003), même négatif combiné, 65f 704x1280 :
la SEULE variable vs clip_A = la FORME du prompt.

Usage : python3 tests/sdcpp_t2v_docprompt_test.py   (~12 min)
Sortie : downloads/docprompt_<date>/clip_doc.webm (+ run.log)
Verdict : à l'œil, vs clip_A (qipao ? stance ? pas d'appart ?).
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
log = logging.getLogger("t2v-docprompt")

RUN_DIR = PIPELINE_ROOT / "downloads" / f"docprompt_{datetime.now():%Y%m%d_%H%M%S}"

PROMPT = (
    "She drops into a confident horse stance as dust bursts around her boots. "
    "A dark-haired martial artist in a blue qipao settles her weight, the fabric "
    "pulling tight across her thighs. A dramatic side light cuts through the air "
    "and makes the drifting dust glow. The camera tilts very slowly downward along "
    "her stance while her fingers flex and a breath shifts her hair. Sweat beads "
    "glisten on her collarbone as she holds the pose, powerful and controlled. "
    "Faint wind and the soft creak of fabric fill the background, no music."
)

NEGATIVE = (
    "blurry face, deformed hands, text, watermark, indoor apartment, selfie, "
    "casual home video, static room, " + SDCPP_DEFAULT_NEGATIVE
)

SEED = 3439573003  # même seed que clip 4 / clip_A -> seule variable = le prompt
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
    out = RUN_DIR / "clip_doc.webm"
    cmd = _build_cmd(out)
    with open(RUN_DIR / "run.log", "a") as f:
        f.write(f"seed={SEED} steps={STEPS} cfg=3.0 guidance=3.5\n")
        f.write(f"prompt={PROMPT}\nnegative={NEGATIVE}\n")
    log.info(f"seed={SEED} -> {out}")

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
        print("DOCPROMPT: ECHEC (timeout)")
        raise SystemExit(1)

    ret = await proc.wait()
    if ret != 0 or not out.is_file():
        log.error(f"exited {ret}\n" + buf.decode(errors="replace")[-4000:])
        print("DOCPROMPT: ECHEC")
        raise SystemExit(1)
    log.info(f"DONE -> {out} ({out.stat().st_size} o)")
    print(f"DOCPROMPT: OK -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
