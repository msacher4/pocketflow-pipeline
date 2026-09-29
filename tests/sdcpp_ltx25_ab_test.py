"""Test LTX 2.5 via stable-diffusion.cpp — specs identiques au workflow (T2V).

Variante A : sans --embeddings-connectors
Variante B : avec --embeddings-connectors (le "with-proj" les rend peut-être inutiles)

Usage : python tests/sdcpp_ltx25_ab_test.py A|B
Sortie dans output/ltx25_ab_test/
"""

import asyncio
import logging
import os
import random
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from config import SDCPP_ROCM_LIB

# Nouveau build sd-cli compatible LTX-2.5 (afd5306), build actuel non touché
SDCPP_BIN = Path(os.getenv(
    "SDCLI_LTX25_BIN",
    "/media/marcs/Linux_Apps/stable-diffusion.cpp_ltx25/build_ltx25/bin/sd-cli",
))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ltx25-ab-test")

OUT_DIR = PIPELINE_ROOT / "output" / "ltx25_ab_test"

LTX25 = Path("/media/marcs/Linux_Apps/LLM/LTX2.5")
DIFFUSION = LTX25 / "ltx-2.5-22b-distilled-transformer-Q4_0.gguf"
LLM = LTX25 / "Encoder" / "gemma4-12b-with-proj-ltx-2.5-Q4_K_M.gguf"
VAE = LTX25 / "Vae" / "ltx-2.5-video-vae-conv-bf16.safetensors"
# Pas de connectors dédié pour 2.5 (proj dans le GGUF) ; on teste la variante B avec le
# connector LTX2.3 d'ailleurs réutilisable comme référence de comportement.
CONNECTORS = Path("/media/marcs/Linux_Apps/Projets_AI/stable-diffusion.cpp/models/embeddings_connectors.safetensors")

PROMPT = (
    "cinematic 9:16 portrait shot, a powerful warrior raising a glowing sword at dawn "
    "on a cliff, slow dolly-in camera, cape flowing in the wind, embers rising, "
    "dynamic cinematic camera movement with slow dramatic pan and parallax, subject "
    "in dynamic action, flowing hair and cloth, energetic motion, always in motion, never static"
)

# Specs exactes du workflow (sdcpp_api._build_cmd / config.py)
FRAMES, WIDTH, HEIGHT, STEPS, CFG = 65, 704, 1280, 8, 1.0


def _env() -> dict:
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = SDCPP_ROCM_LIB
    env["GGML_CUDA_DISABLE_GRAPHS"] = "1"
    return env


def _build_cmd(variant: str, output: Path, seed: int) -> list[str]:
    cmd = [
        str(SDCPP_BIN),
        "-M", "vid_gen",
        "--diffusion-model", str(DIFFUSION),
        "--vae", str(VAE),
        "--llm", str(LLM),
        "--backend", "te=ROCm0,diffusion=ROCm0,vae=ROCm0",
        "--params-backend", "te=cpu,diffusion=ROCm0,vae=ROCm0",
        "--max-vram", "15",
        "--steps", str(STEPS),
        "--cfg-scale", str(CFG),
        "--sampling-method", "euler",
        "--video-frames", str(FRAMES),
        "--width", str(WIDTH),
        "--height", str(HEIGHT),
        "--temporal-tiling",
        "--extra-tiling-args", "temporal_tile_frames=2",
        "--diffusion-fa",
        "--output", str(output),
        "-p", PROMPT,
        "-s", str(seed),
        "-v",
    ]
    if variant == "B":
        cmd += ["--embeddings-connectors", str(CONNECTORS)]
    return cmd


async def run(variant: str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"clip_{variant}.webm"
    seed = random.randint(0, 2**32)
    cmd = _build_cmd(variant, out, seed)
    log.info(f"[{variant}] seed={seed} -> {out}")
    log.info(f"[{variant}] cmd: {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env()
    )
    buf = bytearray()
    try:
        async with asyncio.timeout(2400):
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 200000:  # on ne loggue que la fin
                    buf = buf[-100000:]
    except TimeoutError:
        log.error(f"[{variant}] TIMEOUT, killing")
        proc.terminate()
        await proc.wait()
        return None

    ret = await proc.wait()
    text = buf.decode(errors="replace")
    if ret != 0 or not out.is_file():
        log.error(f"[{variant}] exited {ret}\n" + text[-4000:])
        return None

    size = out.stat().st_size
    # extraire les lignes métriques (VRAM/RTF)
    metrics = [l for l in text.splitlines() if any(k in l for k in
               ("RTF", "vram", "VV:", "VRAM", "total", "generate", "sampl", "et/s", "GB"))]
    log.info(f"[{variant}] DONE -> {out} ({size} o)")
    for m in metrics[-15:]:
        log.info(f"[{variant}] {m.strip()}")
    return str(out)


async def main():
    variant = sys.argv[1].upper() if len(sys.argv) > 1 else "A"
    if variant not in ("A", "B"):
        print("Variante invalide (A ou B)")
        raise SystemExit(2)
    path = await run(variant)
    if not path:
        print(f"VARIANTe {variant}: ECHEC")
        raise SystemExit(1)
    print(f"VARIANTe {variant}: OK -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
