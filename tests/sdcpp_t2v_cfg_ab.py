"""A/B test réglages T2V LTX-2.5 — même prompt statique, seuls les réglages changent.

Contexte : les clips 4/5 (poses statiques) sont sortis génériques alors que le
clip 3 (action) suivait son prompt, à réglages identiques. On tranche :
- Bras A = prod actuelle (8 steps, cfg-scale 3.0, guidance 3.5)
- Bras B = recette du test A/B d'origine (8 steps, cfg 1.0, SANS guidance)

Tout le reste est FIGÉ (même prompt, même seed, même négatif, mêmes modèle/
résolution/frames) : la différence vidéo ne peut venir que des réglages.

Usage :
  python3 tests/sdcpp_t2v_cfg_ab.py A      # bras A seul (~12 min)
  python3 tests/sdcpp_t2v_cfg_ab.py B      # bras B seul (~12 min)
Sorties : downloads/ab_cfg_<AAAAMMJJ_HHMMSS>/clip_A.webm, clip_B.webm (+ run.log)
Verdict : à l'œil (qipao ? horse stance ? pas d'appart ?).

Ne PAS ajouter de bras 12 steps (décision utilisateur) ni de scoring auto.
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
log = logging.getLogger("t2v-cfg-ab")

RUN_DIR = PIPELINE_ROOT / "downloads" / f"ab_cfg_{datetime.now():%Y%m%d_%H%M%S}"

# Prompt EXACT du clip 4 raté (Video: du script, sans le préfixe) + suffixe forcé
# par la prod (sdcpp_video_generator.py) — on teste la réalité telle quelle.
PROMPT = (
    "A dark-haired martial artist in a blue qipao standing in a confident horse "
    "stance, the fabric pulling tight across powerful thighs as she shifts her "
    "weight subtly, dramatic side lighting making dust particles glow in the air, "
    "very slow tilt downward across her stance, her breathing visible and controlled "
    "[0-1s: the horse stance is held, the blue qipao fabric pulls tight across her "
    "thighs as she settles her weight] [1-2s: a very slow tilt begins downward, dust "
    "particles float and catch the side light] [2-3s: her fingers flex slightly and "
    "her hair shifts with a subtle breath, the fabric creases along her leg] [3-4s: "
    "the slow tilt settles lower, her stance remains powerful, sweat beads glisten "
    "along her collarbone and the fabric moves with her breathing]. "
    "Dynamic cinematic camera movement with slow dramatic pan and parallax, subject "
    "in dynamic action, flowing hair and cloth, energetic motion, always in motion, "
    "never static."
)

# Négatif FIGÉ pour les deux bras (= assemblage prod : négatif type blueprint +
# défauts officiels LTX2.5). Le négatif blueprint exact du run est perdu
# (mémoire effacée) ; on utilise un négatif représentatif.
NEGATIVE = (
    "blurry face, deformed hands, text, watermark, indoor apartment, selfie, "
    "casual home video, static room, " + SDCPP_DEFAULT_NEGATIVE
)

SEED = 3439573003  # seed du clip 4 raté -> comparaison directe avant/après
STEPS = 8
FRAMES, WIDTH, HEIGHT = SDCPP_FRAMES, SDCPP_WIDTH, SDCPP_HEIGHT

ARMS = {
    # Bras A : prod actuelle au moment du test
    "A": {"cfg": "3.0", "guidance": True},
    # Bras B : recette du test A/B d'origine (tests/sdcpp_ltx25_ab_test.py)
    "B": {"cfg": "1.0", "guidance": False},
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
        f.write(f"[{arm}] negative={NEGATIVE}\n")
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
    arm = sys.argv[1].upper() if len(sys.argv) > 1 else "A"
    if arm not in ARMS:
        print("Bras invalide (A ou B)")
        raise SystemExit(2)
    path = await run(arm)
    if not path:
        print(f"BRAS {arm}: ECHEC")
        raise SystemExit(1)
    print(f"BRAS {arm}: OK -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
