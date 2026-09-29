import asyncio
import logging
import os
import random
from pathlib import Path

from config import (
    SDCPP_BIN,
    SDCPP_BIN_I2V,
    SDCPP_ROCM_LIB,
    SDCPP_LTX25_DIFFUSION,
    SDCPP_LTX25_VAE,
    SDCPP_LTX25_LLM,
    SDCPP_LTX25_AUDIO_VAE,
    SDCPP_HIRES_DIR,
    SDCPP_HIRES_MODEL,
    SDCPP_FRAMES,
    SDCPP_WIDTH,
    SDCPP_HEIGHT,
    SDCPP_FPS,
    SDCPP_STEPS,
    SDCPP_TILE_FRAMES,
    SDCPP_TIMEOUT,
    SDCPP_I2V_WIDTH,
    SDCPP_I2V_HEIGHT,
    SDCPP_I2V_FRAMES,
    SDCPP_I2V_STEPS,
    SDCPP_I2V_CFG,
    SDCPP_I2V_TILE_FRAMES,
    SDCPP_I2V_TILE_OVERLAP,
)

log = logging.getLogger("pocketflow-pipeline")

# Keywords négatifs recommandés par la doc officielle LTX2.5 — base TOUJOURS
# présente (le négatif du LLM s'y AJOUTE, ne la remplace jamais).
SDCPP_DEFAULT_NEGATIVE = "worst quality, low quality, blurry, distorted, artifacts"


def _sdcli_env() -> dict:
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = SDCPP_ROCM_LIB
    return env


def _build_cmd(prompt: str, output: Path, seed: int, frames: int, width: int, height: int, steps: int,
               negative: str | None = None, fps: int = SDCPP_FPS,
               tile_frames: int = SDCPP_TILE_FRAMES) -> list[str]:
    """Commande sd-cli T2V LTX-2.5 — spec validée sword_640x1152_r4_seed1234567.

    width/height = passe BASE. Le hires latent x2 (/SDCPP_HIRES_MODEL) produit
    la sortie finale 2x, puis refine en SDCPP_HIRES_STEPS steps.
    """
    return [
        SDCPP_BIN,
        "-M", "vid_gen",
        "--diffusion-model", SDCPP_LTX25_DIFFUSION,
        "--vae", SDCPP_LTX25_VAE,
        "--audio-vae", SDCPP_LTX25_AUDIO_VAE,
        "--llm", SDCPP_LTX25_LLM,
        "--backend", "te=ROCm0,diffusion=ROCm0,vae=ROCm0",
        "--params-backend", "te=cpu,diffusion=ROCm0,vae=ROCm0",
        "--max-vram", "15",
        "--steps", str(steps),
        "--cfg-scale", "3.0",
        "--sampling-method", "euler",
        "-n", negative if negative else SDCPP_DEFAULT_NEGATIVE,
        "--video-frames", str(frames),
        "--fps", str(fps),
        "--width", str(width),
        "--height", str(height),
        "--temporal-tiling",
        "--extra-tiling-args", f"temporal_tile_frames={tile_frames}",
        "--diffusion-fa",
        "--hires",
        "--hires-upscalers-dir", SDCPP_HIRES_DIR,
        "--hires-upscaler", SDCPP_HIRES_MODEL,
        "--hires-steps", "4",
        "--hires-upscale-tile-size", "128",
        "--output", str(output),
        "-p", prompt,
        "-s", str(seed),
        "-v",
    ]


async def sdcpp_generate_video_t2v(
    prompt: str,
    dest_dir: Path,
    name: str,
    seed: int | None = None,
    frames: int = SDCPP_FRAMES,
    width: int = SDCPP_WIDTH,
    height: int = SDCPP_HEIGHT,
    steps: int = SDCPP_STEPS,
    timeout_s: int = SDCPP_TIMEOUT,
    negative: str | None = None,
) -> str | None:
    """Génère un clip vidéo T2V via stable-diffusion.cpp (LTX-2.5 22B distill).

    Lance sd-cli en subprocess asynchrone et cancellable. Retourne le chemin
    du fichier .webm généré, ou None en cas d'échec.
    """
    if not prompt or not prompt.strip():
        log.error("sdcpp_t2v: empty prompt")
        return None
    if not Path(SDCPP_BIN).is_file():
        log.error(f"sdcpp_t2v: binary not found: {SDCPP_BIN}")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    output = dest_dir / f"{name}.webm"
    seed = seed if seed is not None else random.randint(0, 2**32)

    cmd = _build_cmd(prompt, output, seed, frames, width, height, steps,
                     negative=negative)
    log.info(f"sdcpp_t2v: {name} seed={seed} frames={frames} {width}x{height} steps={steps}")

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_sdcli_env(),
        )
        stdout_buf = bytearray()
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    chunk = await proc.stdout.read(8192)
                    if not chunk:
                        break
                    stdout_buf.extend(chunk)
                    log.info(f"sdcpp_t2v: {name} progress: {len(stdout_buf)} bytes logged")
        except TimeoutError:
            log.error(f"sdcpp_t2v: {name} TIMEOUT after {timeout_s}s, killing")
            proc.terminate()
            await proc.wait()
            return None

        ret = await proc.wait()
        if ret != 0:
            log.error(f"sdcpp_t2v: {name} exited {ret} — tail:\n" + stdout_buf.decode(errors="replace")[-2000:])
            return None
    except asyncio.CancelledError:
        log.warning(f"sdcpp_t2v: {name} cancelled, killing subprocess")
        if proc is not None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:
                proc.kill()
        raise
    except FileNotFoundError:
        log.error(f"sdcpp_t2v: {name} failed to launch {SDCPP_BIN}")
        return None

    if not output.is_file():
        log.error(f"sdcpp_t2v: {name} output missing: {output}")
        return None

    size = output.stat().st_size
    log.info(f"sdcpp_t2v: {name} done -> {output} ({size} bytes)")
    return str(output)


def _build_cmd_i2v(
    prompt: str,
    image_path: str,
    output: Path,
    seed: int,
    frames: int,
    width: int,
    height: int,
    steps: int,
    cfg: float,
) -> list[str]:
    return [
        SDCPP_BIN_I2V,
        "-M", "vid_gen",
        "--diffusion-model", SDCPP_LTX25_DIFFUSION,
        "--vae", SDCPP_LTX25_VAE,
        "--llm", SDCPP_LTX25_LLM,
        "--backend", "te=ROCm0,diffusion=ROCm0,vae=ROCm0",
        "--params-backend", "te=cpu,diffusion=ROCm0,vae=ROCm0",
        "--max-vram", "15",
        "--steps", str(steps),
        "--cfg-scale", str(cfg),
        "--guidance", "3.5",
        "--sampling-method", "euler",
        "--video-frames", str(frames),
        "--width", str(width),
        "--height", str(height),
        "--temporal-tiling",
        "--extra-tiling-args", f"temporal_tile_frames={SDCPP_I2V_TILE_FRAMES},temporal_tile_overlap={SDCPP_I2V_TILE_OVERLAP}",
        "--diffusion-fa",
        "--output", str(output),
        "-i", image_path,
        "-p", prompt,
        "-s", str(seed),
        "--strength", "1.0",
        "-v",
    ]


async def sdcpp_generate_video_i2v(
    prompt: str,
    image_path: str,
    dest_dir: Path,
    name: str,
    seed: int | None = None,
    frames: int = SDCPP_I2V_FRAMES,
    width: int = SDCPP_I2V_WIDTH,
    height: int = SDCPP_I2V_HEIGHT,
    steps: int = SDCPP_I2V_STEPS,
    cfg: float = SDCPP_I2V_CFG,
    timeout_s: int = SDCPP_TIMEOUT,
) -> str | None:
    """Génère un clip vidéo I2V via stable-diffusion.cpp (LTX-2.5 22B distill).

    Même build que le T2V (build_ltx25) — le binaire gère T2V et I2V selon la
    présence de -i. Prend une image statique en entrée (-i) et la transforme en
    vidéo animée. --strength 1.0 est CRITIQUE : préserve la frame 0 (image
    d'origine). Un strength < 1.0 produirait une sortie gris uniforme (bug
    documenté). Specs validées A/B : 8 steps distill, cfg 1.0 + guidance 3.5,
    max-vram 15, tile temporel 2/1, sans embeddings-connectors (with-proj interne).
    """
    import os
    if not prompt or not prompt.strip():
        log.error("sdcpp_i2v: empty prompt")
        return None
    if not image_path or not os.path.isfile(image_path):
        log.error(f"sdcpp_i2v: source image not found: {image_path}")
        return None
    if not Path(SDCPP_BIN).is_file():
        log.error(f"sdcpp_i2v: binary not found: {SDCPP_BIN}")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    output = dest_dir / f"{name}.webm"
    seed = seed if seed is not None else random.randint(0, 2**32)

    # sd-cli charge le VAE + image dans l'input ; s'assurer que l'image est accessible
    cmd = _build_cmd_i2v(prompt, image_path, output, seed, frames, width, height, steps, cfg)
    log.info(f"sdcpp_i2v: {name} seed={seed} frames={frames} {width}x{height} steps={steps} img={os.path.basename(image_path)}")

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_sdcli_env(),
        )
        stdout_buf = bytearray()
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    chunk = await proc.stdout.read(8192)
                    if not chunk:
                        break
                    stdout_buf.extend(chunk)
                    log.info(f"sdcpp_i2v: {name} progress: {len(stdout_buf)} bytes logged")
        except TimeoutError:
            log.error(f"sdcpp_i2v: {name} TIMEOUT after {timeout_s}s, killing")
            proc.terminate()
            await proc.wait()
            return None

        ret = await proc.wait()
        if ret != 0:
            log.error(f"sdcpp_i2v: {name} exited {ret} — tail:\n" + stdout_buf.decode(errors="replace")[-2000:])
            return None
    except asyncio.CancelledError:
        log.warning(f"sdcpp_i2v: {name} cancelled, killing subprocess")
        if proc is not None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:
                proc.kill()
        raise
    except FileNotFoundError:
        log.error(f"sdcpp_i2v: {name} failed to launch {SDCPP_BIN}")
        return None

    if not output.is_file():
        log.error(f"sdcpp_i2v: {name} output missing: {output}")
        return None

    size = output.stat().st_size
    log.info(f"sdcpp_i2v: {name} done -> {output} ({size} bytes)")
    return str(output)
