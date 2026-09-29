import asyncio
import json
import logging
import os
import random
from pathlib import Path

from config import (
    ACESTEP_BIN_LM,
    ACESTEP_BIN_SYNTH,
    ACESTEP_MODELS_DIR,
    ACESTEP_GGML_BACKEND,
    ACESTEP_LM_TIMEOUT,
    ACESTEP_SYNTH_TIMEOUT,
    ACESTEP_STEPS,
    ACESTEP_SHIFT,
    ACESTEP_CFG,
)

log = logging.getLogger("pocketflow-pipeline")


def _ace_cpp_available() -> bool:
    """True si les 2 binaires et les 4 modèles GGUF acestep.cpp sont présents."""
    if not Path(ACESTEP_BIN_LM).is_file() or not Path(ACESTEP_BIN_SYNTH).is_file():
        log.warning("audio_cpp_api: binaries not found (PF_ACESTEP_BIN_LM / PF_ACESTEP_BIN_SYNTH)")
        return False
    models = [p.name for p in Path(ACESTEP_MODELS_DIR).glob("*.gguf")]
    required = {
        "acestep-5Hz-lm-4B-Q5_K_M.gguf",
        "acestep-v15-xl-turbo-Q4_K_M.gguf",
        "vae-BF16.gguf",
    }
    missing = [r for r in required if r not in models]
    if missing:
        log.warning(f"audio_cpp_api: missing ACE-Step models in {ACESTEP_MODELS_DIR}: {missing}")
        return False
    return True


def _ace_cpp_env() -> dict:
    env = os.environ.copy()
    env["GGML_BACKEND"] = ACESTEP_GGML_BACKEND
    return env


def _write_request(probe: dict, req_path: Path) -> Path:
    """Écrit le request JSON pour ace-lm (prompt + métadonnées verrouillées).

    Les métadonnées (bpm/keyscale/timesignature/duration) ne sont jamais
    réécrites par le LM (garanti côté acestep.cpp) → contrôle du mood.

    lyrics vide -> "[Instrumental]" : le LM ajoute `instrumental: true`
    (pipelines acestep.cpp): lyrics="" déclenche le mode "génère des paroles"
    → voix vocale indésirable. "[Instrumental]" force une piste sans voix."""
    req = {
        "caption": probe["prompt"],
        "lyrics": probe.get("lyrics") or "[Instrumental]",
        "duration": int(probe.get("duration", 22)),
        "bpm": int(probe.get("bpm", 120)),
        "keyscale": probe.get("keyscale", "A minor"),
        "timesignature": str(probe.get("timesignature", 4)),
        "inference_steps": ACESTEP_STEPS,
        "shift": ACESTEP_SHIFT,
        "guidance_scale": ACESTEP_CFG,
        "output_format": "mp3",
    }
    req_path.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    return req_path


async def _run_cli(bin_path: Path, args: list[str], timeout_s: int, label: str) -> tuple[int, str]:
    """Lance un binaire acestep.cpp (ace-lm ou ace-synth) en subprocess asynchrone."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(bin_path), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_ace_cpp_env(),
        )
        buf = bytearray()
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    chunk = await proc.stdout.read(8192)
                    if not chunk:
                        break
                    buf.extend(chunk)
        except TimeoutError:
            log.error(f"audio_cpp {label}: TIMEOUT after {timeout_s}s, killing")
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:
                proc.kill()
            return 1, buf.decode(errors="replace")

        ret = await proc.wait()
        return ret, buf.decode(errors="replace")
    except asyncio.CancelledError:
        log.warning(f"audio_cpp {label}: cancelled, killing subprocess")
        if proc is not None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:
                proc.kill()
        raise
    except FileNotFoundError:
        log.error(f"audio_cpp {label}: binary not found: {bin_path}")
        return 1, ""


async def acestep_generate_music(
    prompt: str,
    dest_dir: Path,
    name: str,
    lyrics: str = "",
    bpm: int = 120,
    keyscale: str = "A minor",
    timesignature: int = 4,
    duration: int = 22,
    seed: int | None = None,
) -> str | None:
    """Génère une musique via acestep.cpp (ACE-Step 1.5 en GGUF/Vulkan).

    Chaîne : ace-lm (codes audio) -> ace-synth (DiT + VAE) -> .mp3.
    Retourne le chemin du mp3, ou None en cas d'échec.

    lyrics vide -> instrumental (forcé par "[Instrumental]" dans le request) ;
    passer un texte de paroles pour générer une piste vocale. Ce choix garantit
    l'absence de voix, nécessaire car un voiceover Fish Speech sera superposé.
    """
    if not _ace_cpp_available():
        return None
    if not prompt or not prompt.strip():
        log.error("acestep_music: empty prompt")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    work_dir = dest_dir / f".acestep_{name}"
    work_dir.mkdir(parents=True, exist_ok=True)

    req = _write_request(
        {"prompt": prompt, "lyrics": lyrics, "bpm": bpm, "keyscale": keyscale,
         "timesignature": timesignature, "duration": duration},
        work_dir / "request.json",
    )
    log.info(f"acestep_music: {name} bpm={bpm} {keyscale} dur={duration}s seed={seed}")

    # Phase 1 : LM -> codes audio (req request0.json / request00.json)
    ret, out = await _run_cli(Path(ACESTEP_BIN_LM), ["--models", ACESTEP_MODELS_DIR, "--request", str(req)],
                              ACESTEP_LM_TIMEOUT, f"lm:{name}")
    if ret != 0:
        log.error(f"acestep_music LM {name}: exit {ret} — tail:\n" + out[-2000:])
        return None

    code_req = work_dir / "request0.json"
    if not code_req.is_file():
        # ace-lm écrit <basename>0.json à côté du request si lm_batch_size=1
        log.error(f"acestep_music LM {name}: output request not found ({code_req})")
        return None

    # Phase 2 : DiT + VAE -> mp3 (écrit <basename>00.mp3 à côté)
    mp3 = work_dir / "request00.mp3"
    ret, out = await _run_cli(Path(ACESTEP_BIN_SYNTH), ["--models", ACESTEP_MODELS_DIR, "--request", str(code_req)],
                              ACESTEP_SYNTH_TIMEOUT, f"synth:{name}")
    if ret != 0:
        log.error(f"acestep_music synth {name}: exit {ret} — tail:\n" + out[-2000:])
        return None
    if not mp3.is_file():
        log.error(f"acestep_music synth {name}: output missing: {mp3}")
        # glisser sur le nom réellement produit
        produced = sorted(work_dir.glob("*.mp3"))
        if not produced:
            return None
        mp3 = produced[0]

    final = dest_dir / f"{name}.mp3"
    mp3.rename(final)
    log.info(f"acestep_music: {name} done -> {final} ({final.stat().st_size} bytes)")
    return str(final)