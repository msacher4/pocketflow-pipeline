import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

import httpx

from config import (
    S2_TEMPERATURE,
    S2_TOP_P,
    S2_TOP_K,
    S2_MAX_NEW_TOKENS,
    S2_PITCH_SHIFT,
)

log = logging.getLogger("pocketflow-pipeline")

COMFYUI_URL = "http://127.0.0.1:8188"
COMFYUI_SERVICE = "comfyui.service"
COMFYUI_START_TIMEOUT = 120
REPO_WORKFLOWS_DIR = Path(__file__).parent.parent / "workflows"
WORKFLOWS_DIR = Path(__file__).parent.parent.parent / "mcp" / "comfyui-mcp" / "workflows"
COMFYUI_OUTPUT = Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/output")
COMFYUI_INPUT = Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/input")

# Nombre d'images de référence câblées dans generate_image_klein_ref.json
# (une paire LoadImage -> ImageScaleToTotalPixels -> VAEEncode -> 2 ReferenceLatent).
REF_IMAGE_SLOTS = 2

S2_HOST = "127.0.0.1"
S2_PORT = 5236
S2_URL = f"http://{S2_HOST}:{S2_PORT}"
S2_START_TIMEOUT = 180
S2_BIN = Path("/media/marcs/Linux_Apps/Projets_AI/s2.cpp/build/s2")
S2_GGUF = Path("/media/marcs/Linux_Apps/LLM/FIsh Audio S2 Pro/s2-pro-q8_0.gguf")
S2_TOKENIZER = Path("/media/marcs/Linux_Apps/Projets_AI/s2.cpp/tokenizer.json")
S2_LOG_FILE = Path("/media/marcs/Linux_Apps/Projets_AI/s2.cpp/output/server.log")
S2_VOICE_PROFILES = Path("/media/marcs/Linux_Apps/Projets_AI/s2.cpp/voice_profiles")


async def comfyui_ready() -> bool:
    """True si le serveur ComfyUI répond sur /system_stats."""
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            r = await c.get(f"{COMFYUI_URL}/system_stats")
            return r.status_code == 200
        except Exception:
            return False


async def comfyui_ensure_started() -> None:
    """Démarre ComfyUI à la demande (comme sd-cli) si le serveur ne répond pas.

    Lance le service systemd --user comfyui puis attend qu'il soit prêt.
    """
    if await comfyui_ready():
        return

    log.info(f"ComfyUI not reachable, starting {COMFYUI_SERVICE}...")
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "start", COMFYUI_SERVICE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0:
        log.warning(f"{COMFYUI_SERVICE} start returned {proc.returncode}")

    deadline = asyncio.get_event_loop().time() + COMFYUI_START_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        if await comfyui_ready():
            log.info("ComfyUI ready")
            return
        await asyncio.sleep(2)
    raise TimeoutError(f"ComfyUI not ready after {COMFYUI_START_TIMEOUT}s")


async def comfyui_free(unload_models: bool = True, free_memory: bool = True) -> None:
    """Appelle POST /free pour décharger tous les modèles et libérer la VRAM."""
    await comfyui_ensure_started()
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.post(
                f"{COMFYUI_URL}/free",
                json={"unload_models": unload_models, "free_memory": free_memory},
            )
            log.info(f"/free -> {r.status_code} (unload_models={unload_models}, free_memory={free_memory})")
        except Exception as e:
            log.warning(f"/free failed: {e}")


def _load_workflow(name: str) -> dict:
    """Charge un workflow API-format.

    Le dépôt est prioritaire (workflows versionnés avec le code) ; le dossier
    externe prend le relais pour les workflows historiques qui n'y sont pas.
    """
    for base in (REPO_WORKFLOWS_DIR, WORKFLOWS_DIR):
        path = base / f"{name}.json"
        if path.is_file():
            return json.loads(path.read_text())
    raise FileNotFoundError(f"Workflow not found: {name} (ni {REPO_WORKFLOWS_DIR} ni {WORKFLOWS_DIR})")


def _fill_params(workflow: dict, params: dict) -> dict:
    wf = json.loads(json.dumps(workflow))
    for node_id, node in wf.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        for key, val in inputs.items():
            if isinstance(val, str) and val in params:
                inputs[key] = params[val]
    return wf


async def comfyui_submit(workflow: dict) -> str:
    await comfyui_ensure_started()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{COMFYUI_URL}/prompt", json={"prompt": workflow})
        r.raise_for_status()
        data = r.json()
        prompt_id = data.get("prompt_id", "")
        log.info(f"ComfyUI submitted: {prompt_id}")
        return prompt_id


async def comfyui_poll(prompt_id: str, timeout_s: int = 1800) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    async with httpx.AsyncClient(timeout=30) as c:
        while asyncio.get_event_loop().time() < deadline:
            r = await c.get(f"{COMFYUI_URL}/history/{prompt_id}")
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                entry = data[prompt_id]
                status = entry.get("status", {})
                if status.get("completed", False):
                    log.info(f"ComfyUI completed: {prompt_id}")
                    return entry.get("outputs", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI error: {status}")
            await asyncio.sleep(5)
    raise TimeoutError(f"ComfyUI timeout after {timeout_s}s for {prompt_id}")


def _find_output_video(outputs: dict) -> Path | None:
    for node_id, node_out in outputs.items():
        videos = node_out.get("videos", []) or node_out.get("gifs", [])
        for v in videos:
            filename = v.get("filename", "")
            subfolder = v.get("subfolder", "")
            candidate = COMFYUI_OUTPUT / subfolder / filename
            if candidate.is_file():
                return candidate
        animated = node_out.get("animated") or []
        images = node_out.get("images", [])
        for i, img in enumerate(images):
            filename = img.get("filename", "")
            if filename.lower().endswith((".mp4", ".webm", ".gif")) or (animated[i] if i < len(animated) else False):
                subfolder = img.get("subfolder", "")
                candidate = COMFYUI_OUTPUT / subfolder / filename
                if candidate.is_file():
                    return candidate
    return None


def _find_output_image(outputs: dict) -> Path | None:
    for node_id, node_out in outputs.items():
        images = node_out.get("images", [])
        for img in images:
            filename = img.get("filename", "")
            subfolder = img.get("subfolder", "")
            candidate = COMFYUI_OUTPUT / subfolder / filename
            if candidate.is_file():
                return candidate
    return None


def _find_output_audio(outputs: dict) -> Path | None:
    """Trouve le premier fichier audio produit (SaveAudioMP3 → clé 'audio')."""
    for node_id, node_out in outputs.items():
        audio = node_out.get("audio", []) or node_out.get("audios", [])
        for a in audio:
            filename = a.get("filename", "")
            subfolder = a.get("subfolder", "")
            candidate = COMFYUI_OUTPUT / subfolder / filename
            if candidate.is_file():
                return candidate
    return None


def _copy_to_dest(src: Path, dest_dir: Path, name: str) -> str:
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix or ".mp4"
    dest = dest_dir / f"{name}{ext}"
    shutil.copy2(src, dest)
    log.info(f"Copied {src.name} -> {dest}")
    return str(dest)


async def comfyui_generate_image(
    prompt: str,
    negative_prompt: str,
    dest_dir: Path,
    name: str,
    seed: int | None = None,
    width: int = 1024,
    height: int = 1024,
    steps: int = 20,
    cfg: float = 3.5,
) -> str | None:
    import random
    wf = _load_workflow("generate_image_klein")
    params = {
        "PARAM_PROMPT": prompt,
        "PARAM_NEGATIVE_PROMPT": negative_prompt or "blurry, low quality, text, watermark",
        "PARAM_INT_SEED": seed if seed is not None else random.randint(0, 2**32),
        "PARAM_INT_STEPS": steps,
        "PARAM_FLOAT_CFG": cfg,
        "PARAM_STR_SAMPLER_NAME": "euler",
        "PARAM_STR_SCHEDULER": "normal",
        "PARAM_FLOAT_DENOISE": 1.0,
        "PARAM_INT_WIDTH": width,
        "PARAM_INT_HEIGHT": height,
        "PARAM_MODEL": "flux-2-klein-4b-Q8_0.gguf",
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=300)
    img_path = _find_output_image(outputs)
    if not img_path:
        log.error(f"ComfyUI image: no output found for {prompt_id}")
        return None
    return _copy_to_dest(img_path, dest_dir, name)


async def comfyui_generate_image_ref(
    prompt: str,
    negative_prompt: str,
    reference_images: list[str],
    dest_dir: Path,
    name: str,
    seed: int | None = None,
    width: int = 320,
    height: int = 576,
    steps: int = 4,
    cfg: float = 1.0,
) -> str | None:
    """Génère une image Klein 4B guidée par jusqu'à 2 images de RÉFÉRENCE.

    Topologie calquée sur le workflow officiel `Image Edit (Flux.2 Klein 4B
    Distilled)` : EmptyFlux2LatentImage 128ch/16, Flux2Scheduler,
    RandomNoise + KSamplerSelect + CFGGuider + SamplerCustomAdvanced,
    références à 1 MP en `nearest-exact`, négatif obtenu par
    ConditioningZeroOut sur le positif.

    `steps=4` et `cfg=1.0` ne sont pas choisis au hasard : ce sont les valeurs
    du modèle DISTILLED (flux-2-klein-4b, 4 steps). Le modèle base en veut 25/4.0.

    `negative_prompt` est conservé pour la compatibilité d'appel mais IGNORÉ :
    le workflow officiel ne fait pas d'encodage négatif séparé.

    Le canvas reste vide : le prompt décrit la scène, les références
    n'apportent que l'identité. C'est ce qui distingue cette fonction du mode
    "édition", où la référence est encodée comme canvas et où la composition
    d'origine est conservée.

    `reference_images` est limitée à 2 entrées (nombre de nœuds
    ReferenceLatent du workflow). Un seul chemin est accepté : la référence est
    dupliquée.
    """
    import random

    refs = [str(r) for r in reference_images if r and Path(r).is_file()]
    if not refs:
        log.error(f"ComfyUI image ref: aucune image de référence valide pour {name}")
        return None
    while len(refs) < REF_IMAGE_SLOTS:
        refs.append(refs[-1])

    wf = _load_workflow("generate_image_klein_ref")
    params = {
        "PARAM_PROMPT": prompt,
        "PARAM_NEGATIVE_PROMPT": negative_prompt or "blurry, low quality, text, watermark",
        "PARAM_INT_SEED": seed if seed is not None else random.randint(0, 2**32),
        "PARAM_INT_STEPS": steps,
        "PARAM_FLOAT_CFG": cfg,
        "PARAM_STR_SAMPLER_NAME": "euler",
        "PARAM_STR_SCHEDULER": "normal",
        "PARAM_FLOAT_DENOISE": 1.0,
        "PARAM_INT_WIDTH": width,
        "PARAM_INT_HEIGHT": height,
        "PARAM_MODEL": "flux-2-klein-4b-Q8_0.gguf",
    }
    for i, src in enumerate(refs[:REF_IMAGE_SLOTS], start=1):
        copied = COMFYUI_INPUT / f"klein_ref_{name}_{i}{Path(src).suffix}"
        shutil.copy2(src, copied)
        log.info(f"Reference {i} -> {copied}")
        params[f"PARAM_REF_IMAGE_{i}"] = copied.name

    filled = _fill_params(wf, params)
    unresolved = {
        k for node in filled.values() if isinstance(node, dict)
        for k, v in node.get("inputs", {}).items()
        if isinstance(v, str) and v.startswith("PARAM_")
    }
    if unresolved:
        raise RuntimeError(f"Workflow klein_ref: PARAM_ non résolus : {sorted(unresolved)}")

    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=600)
    img_path = _find_output_image(outputs)
    if not img_path:
        log.error(f"ComfyUI image ref: no output found for {prompt_id}")
        return None
    return _copy_to_dest(img_path, dest_dir, name)


async def comfyui_generate_video(
    prompt: str,
    negative_prompt: str,
    start_image: str,
    dest_dir: Path,
    name: str,
    seed: int | None = None,
    width: int = 1280,
    height: int = 704,
    length: int = 153,
    fps: float = 25.0,
) -> str | None:
    import random

    src = Path(start_image)
    if not src.is_file():
        log.error(f"comfyui_video: source image not found: {src}")
        return None

    dest = COMFYUI_INPUT / src.name
    shutil.copy2(str(src), str(dest))

    wf = _load_workflow("generate_video_continuation")
    params = {
        "PARAM_PROMPT": prompt,
        "PARAM_NEGATIVE_PROMPT": negative_prompt or "blurry, low quality, text, watermark",
        "PARAM_INT_SEED": seed if seed is not None else random.randint(0, 2**32),
        "PARAM_INT_STEPS": 8,
        "PARAM_FLOAT_CFG": 1.0,
        "PARAM_STR_SAMPLER_NAME": "euler",
        "PARAM_FLOAT_FPS": fps,
        "PARAM_INT_WIDTH": width,
        "PARAM_INT_HEIGHT": height,
        "PARAM_INT_LENGTH": length,
        "PARAM_START_IMAGE": src.name,
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=1800)
    vid_path = _find_output_video(outputs)
    if not vid_path:
        log.error(f"ComfyUI video: no output found for {prompt_id}")
        return None
    return _copy_to_dest(vid_path, dest_dir, name)


async def comfyui_generate_video_fast(
    prompt: str,
    negative_prompt: str,
    dest_dir: Path,
    name: str,
    seed: int | None = None,
    width: int = 512,
    height: int = 288,
    length: int = 153,
    fps: float = 15.0,
    upscale_factor: int = 2,
) -> str | None:
    import random

    wf = _load_workflow("generate_video_fast")
    params = {
        "PARAM_PROMPT": prompt,
        "PARAM_NEGATIVE_PROMPT": negative_prompt or "blurry, low quality, text, watermark",
        "PARAM_INT_SEED": seed if seed is not None else random.randint(0, 2**32),
        "PARAM_INT_STEPS": 8,
        "PARAM_FLOAT_CFG": 1.0,
        "PARAM_STR_SAMPLER_NAME": "euler",
        "PARAM_FLOAT_FPS": fps,
        "PARAM_INT_WIDTH": width,
        "PARAM_INT_HEIGHT": height,
        "PARAM_INT_LENGTH": length,
        "PARAM_FLOAT_OUTPUT_FPS": fps * upscale_factor,
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=600)
    vid_path = _find_output_video(outputs)
    if not vid_path:
        log.error(f"ComfyUI video fast: no output found for {prompt_id}")
        return None
    return _copy_to_dest(vid_path, dest_dir, name)


async def comfyui_generate_video_i2v(
    prompt: str,
    negative_prompt: str,
    start_image: str,
    dest_dir: Path,
    name: str,
    seed: int | None = None,
    width: int = 512,
    height: int = 288,
    length: int = 97,
    steps: int = 8,
    cfg: float = 1.0,
    fps: float = 25.0,
) -> str | None:
    import random

    src = Path(start_image)
    if not src.is_file():
        log.error(f"comfyui_video_i2v: source image not found: {src}")
        return None

    dest = COMFYUI_INPUT / src.name
    shutil.copy2(str(src), str(dest))

    wf = _load_workflow("generate_video_i2v")
    params = {
        "PARAM_PROMPT": prompt,
        "PARAM_NEGATIVE_PROMPT": negative_prompt or "blurry, low quality, text, watermark",
        "PARAM_INT_SEED": seed if seed is not None else random.randint(0, 2**32),
        "PARAM_INT_STEPS": steps,
        "PARAM_FLOAT_CFG": cfg,
        "PARAM_STR_SAMPLER_NAME": "euler",
        "PARAM_FLOAT_FPS": fps,
        "PARAM_INT_WIDTH": width,
        "PARAM_INT_HEIGHT": height,
        "PARAM_INT_LENGTH": length,
        "PARAM_START_IMAGE": src.name,
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=1800)
    vid_path = _find_output_video(outputs)
    if not vid_path:
        log.error(f"ComfyUI video i2v: no output found for {prompt_id}")
        return None
    return _copy_to_dest(vid_path, dest_dir, name)


async def comfyui_upscale_video(
    video_path: str,
    dest_dir: Path,
    name: str,
) -> str | None:
    src = Path(video_path)
    if not src.is_file():
        log.error(f"comfyui_upscale_video: source video not found: {src}")
        return None

    dest = COMFYUI_INPUT / src.name
    shutil.copy2(str(src), str(dest))

    wf = _load_workflow("upscale_rife")
    params = {
        "PARAM_VIDEO_FILENAME": src.name,
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=1800)
    vid_path = _find_output_video(outputs)
    if not vid_path:
        log.error(f"ComfyUI upscale: no output found for {prompt_id}")
        return None
    return _copy_to_dest(vid_path, dest_dir, name)


async def comfyui_rife_interp(
    video_path: str,
    dest_dir: Path,
    name: str,
) -> str | None:
    """RIFE ×2 FPS interpolation (no spatial upscale) via ComfyUI."""
    src = Path(video_path)
    if not src.is_file():
        log.error(f"comfyui_rife_interp: source video not found: {src}")
        return None

    dest = COMFYUI_INPUT / src.name
    shutil.copy2(str(src), str(dest))

    wf = _load_workflow("rife_only")
    params = {
        "PARAM_VIDEO_FILENAME": src.name,
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=1800)
    vid_path = _find_output_video(outputs)
    if not vid_path:
        log.error(f"ComfyUI RIFE: no output found for {prompt_id}")
        return None
    return _copy_to_dest(vid_path, dest_dir, name)


async def comfyui_generate_sfx(
    prompt: str,
    dest_dir: Path,
    name: str,
    seconds: float = 2.0,
    negative_prompt: str = "music, melody, singing, speech",
    seed: int | None = None,
    steps: int = 50,
    cfg: float = 7.0,
    sampler_name: str = "dpmpp_2m",
    scheduler: str = "karras",
) -> str | None:
    """Génère un effet sonore via Stable Audio 3 (stable_audio_3_small_sfx).

    Workflow generate_sfx.json : prompt → CLIP (t5gemma) → KSampler → VAE decode → mp3.
    """
    import random
    wf = _load_workflow("generate_sfx")
    params = {
        "PARAM_PROMPT": prompt,
        "PARAM_NEGATIVE_PROMPT": negative_prompt or "",
        "PARAM_FLOAT_SECONDS": seconds,
        "PARAM_INT_SEED": seed if seed is not None else random.randint(0, 2**32),
        "PARAM_INT_STEPS": steps,
        "PARAM_FLOAT_CFG": cfg,
        "PARAM_STR_SAMPLER_NAME": sampler_name,
        "PARAM_STR_SCHEDULER": scheduler,
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=600)
    audio_path = _find_output_audio(outputs)
    if not audio_path:
        log.error(f"ComfyUI sfx: no audio output found for {prompt_id}")
        return None
    return _copy_to_dest(audio_path, dest_dir, name)


async def comfyui_generate_song_15(
    tags: str,
    lyrics: str,
    dest_dir: Path,
    name: str,
    seconds: float = 60.0,
    seed: int | None = None,
    steps: int = 8,
    cfg: float = 2.0,
) -> str | None:
    """Génère une musique via ACE-Step 1.5 (workflow generate_song_15.json).

    Nœuds 1.5 : UNETLoader + ModelSamplingAuraFlow + DualCLIPLoader (2× Qwen)
    + VAELoader + TextEncodeAceStepAudio1.5 → KSampler → VAEDecodeAudio."""
    import random
    wf = _load_workflow("generate_song_15")
    params = {
        "PARAM_TAGS": tags,
        "PARAM_LYRICS": lyrics,
        "PARAM_FLOAT_SECONDS": seconds,
        "PARAM_FLOAT_CFG": cfg,
        "PARAM_INT_SEED": seed if seed is not None else random.randint(0, 2**32),
        "PARAM_INT_STEPS": steps,
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=1800)
    audio_path = _find_output_audio(outputs)
    if not audio_path:
        log.error(f"ComfyUI song 1.5: no audio output found for {prompt_id}")
        return None
    return _copy_to_dest(audio_path, dest_dir, name)


VOICE_REF_SUB = "ref_voice"
VOICE_REF_FILE = "ref_voice.wav"
VOICE_REF_TEXT = (
    "LINNELL'S PICTURES ARE A SORT OF UP GUARDS AND AT EM PAINTINGS AND MASON'S "
    "EXQUISITE IDYLLS ARE AS NATIONAL AS A JINGO POEM MISTER BIRKET FOSTER'S "
    "LANDSCAPES SMILE AT ONE MUCH IN THE SAME WAY THAT MISTER CARKER USED TO FLASH "
    "HIS TEETH AND MISTER JOHN COLLIER GIVES HIS SITTER A CHEERFUL SLAP ON THE BACK "
    "BEFORE HE SAYS LIKE A SHAMPOOER IN A TURKISH BATH NEXT MAN"
)


async def comfyui_generate_voice(
    text: str,
    dest_dir: Path,
    name: str,
    ref_audio: Path | None = None,
    seed: int | None = None,
    temperature: float = 0.7,
    top_p: float = 0.7,
) -> str | None:
    """Génère une voix off via Fish Speech 1.5 + ComfyUI (workflow generate_voice.json).

    Zero-shot TTS : clone la voix de référence (10-30s, transcriptions dans
    VOICE_REF_TEXT). Copie la référence dans l'input ComfyUI si nécessaire."""
    import random

    if not text or not text.strip():
        log.warning("comfyui_voice: empty text, skipping")
        return None

    src = ref_audio or COMFYUI_INPUT / VOICE_REF_SUB / VOICE_REF_FILE
    if not src.is_file():
        log.warning(f"comfyui_voice: reference voice not found: {src}")
        return None

    ref_dir = COMFYUI_INPUT / VOICE_REF_SUB
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_dest = ref_dir / VOICE_REF_FILE
    if not ref_dest.is_file():
        shutil.copy2(src, ref_dest)

    wf = _load_workflow("generate_voice")
    params = {
        "PARAM_REF_AUDIO": f"{VOICE_REF_SUB}/{VOICE_REF_FILE}",
        "PARAM_REF_TEXT": VOICE_REF_TEXT,
        "PARAM_TEXT": text.strip(),
        "PARAM_INT_SEED": seed if seed is not None else random.randint(0, 2**32),
        "PARAM_FLOAT_TEMPERATURE": temperature,
        "PARAM_FLOAT_TOP_P": top_p,
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=900)
    audio_path = _find_output_audio(outputs)
    if not audio_path:
        log.error(f"ComfyUI voice: no audio output found for {prompt_id}")
        return None
    return _copy_to_dest(audio_path, dest_dir, name)


async def s2_ready() -> bool:
    """True si le serveur s2.cpp répond sur son port (pas de route /health)."""
    try:
        reader, writer = await asyncio.open_connection(S2_HOST, S2_PORT)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, ConnectionRefusedError):
        return False


async def s2_ensure_started() -> None:
    """Démarre le serveur s2.cpp (Fish Speech S2) à la demande s'il ne répond pas."""
    if await s2_ready():
        return
    missing = [str(p) for p in (S2_BIN, S2_GGUF, S2_TOKENIZER) if not p.is_file()]
    if missing:
        raise RuntimeError(f"s2.cpp incomplet, manquant: {missing}")

    S2_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logf = open(S2_LOG_FILE, "a")
    log.info(f"s2.cpp not reachable, starting server on {S2_PORT}...")
    proc = await asyncio.create_subprocess_exec(
        str(S2_BIN), "--server",
        "--model", str(S2_GGUF),
        "--tokenizer", str(S2_TOKENIZER),
        "-v", "0",
        "-H", S2_HOST, "-P", str(S2_PORT),
        stdout=logf, stderr=logf,
    )
    logf.close()

    deadline = asyncio.get_event_loop().time() + S2_START_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        if await s2_ready():
            log.info("s2.cpp server ready")
            return
        await asyncio.sleep(2)
    raise TimeoutError(f"s2.cpp not ready after {S2_START_TIMEOUT}s")


def pitch_shift_down(src: Path, dst: Path, semitones: float = -1) -> Path:
    """Abaisse/élève la hauteur de `semitones` demi-tons en préservant la durée.

    Factor = 2^(semitones/12). 0 = pas de modification. Utilise asetrate +
    aresample + atempo (ffmpeg) pour garder la durée d'origine."""
    if not semitones or semitones == 0:
        return src
    factor = 2 ** (semitones / 12)
    if factor <= 0:
        return src
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    )
    try:
        sr = int(probe.stdout.strip())
    except ValueError:
        sr = 44100
    new_rate = int(round(sr * factor))
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-af", f"asetrate={new_rate},aresample={sr},atempo=1/{factor:.6f}",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dst.is_file():
        raise RuntimeError(f"pitch shift ({semitones} st) failed: {r.stderr[:500]}")
    return dst


async def s2_generate_voice(
    text: str,
    dest_dir: Path,
    name: str,
    ref_audio: Path | None = None,
    ref_text: str = VOICE_REF_TEXT,
    voice: str | None = None,
    voice_dir: Path | None = None,
    temperature: float = S2_TEMPERATURE,
    top_p: float = S2_TOP_P,
    top_k: int = S2_TOP_K,
    max_new_tokens: int = S2_MAX_NEW_TOKENS,
    pitch_shift: float = S2_PITCH_SHIFT,
) -> str | None:
    """Génère une voix off via Fish Speech S2 (s2.cpp, POST /generate multipart).

    Deux modes :
    - `voice` + `voice_dir` : utilise un profil .s2voice sauvegardé (recommandé).
    - sinon clone la référence (ref_audio + ref_text) comme la voie 1.5.
    Sort un WAV écrit directement dans dest_dir. Démarre le serveur à la demande."""
    if not text or not text.strip():
        log.warning("s2_voice: empty text, skipping")
        return None

    # temperature <= 0 => défaut config (permet au workflow de ne pas l'imposer)
    temp = temperature if temperature > 0 else S2_TEMPERATURE
    params = json.dumps({
        "temperature": temp,
        "top_p": top_p,
        "top_k": top_k,
        "max_new_tokens": max_new_tokens,
    })

    if voice:
        vdir = voice_dir or S2_VOICE_PROFILES
        profile = vdir / f"{voice}.s2voice"
        if not profile.is_file():
            log.warning(f"s2_voice: voice profile not found: {profile}")
            return None
        await s2_ensure_started()
        files = {
            "text": (None, text.strip()),
            "voice": (None, voice),
            "voice_dir": (None, str(vdir)),
            "params": (None, params),
        }
    else:
        src = ref_audio or (COMFYUI_INPUT / VOICE_REF_SUB / VOICE_REF_FILE)
        if not src.is_file():
            log.warning(f"s2_voice: reference voice not found: {src}")
            return None
        if not ref_text.strip():
            log.warning("s2_voice: reference text vide")
            return None
        await s2_ensure_started()
        files = {
            "text": (None, text.strip()),
            "reference": (src.name, src.read_bytes(), "audio/wav"),
            "reference_text": (None, ref_text.strip()),
            "params": (None, params),
        }
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.post(f"{S2_URL}/generate", files=files)
        r.raise_for_status()
        wav = r.content

    if not wav.startswith(b"RIFF"):
        log.error(f"s2_voice: réponse non-WAV ({len(wav)} bytes)")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.wav"
    dest.write_bytes(wav)
    log.info(f"s2_voice: {name}.wav -> {dest} ({len(wav)} bytes)")

    if pitch_shift and pitch_shift != 0:
        shifted = dest_dir / f"{name}_pitch.wav"
        pitch_shift_down(dest, shifted, pitch_shift)
        dest.write_bytes(shifted.read_bytes())
        shifted.unlink(missing_ok=True)
        log.info(f"s2_voice: {name}.wav pitch-shift {pitch_shift:+g} st")

    return str(dest)


async def comfyui_generate_song(
    tags: str,
    lyrics: str,
    dest_dir: Path,
    name: str,
    seconds: int = 60,
    lyrics_strength: float = 0.99,
    seed: int | None = None,
    steps: int = 50,
    cfg: float = 5.0,
) -> str | None:
    """Génère une musique via ACE-Step (workflow generate_song.json)."""
    import random
    wf = _load_workflow("generate_song")
    params = {
        "PARAM_TAGS": tags,
        "PARAM_LYRICS": lyrics,
        "PARAM_FLOAT_LYRICS_STRENGTH": lyrics_strength,
        "PARAM_INT_SECONDS": seconds,
        "PARAM_INT_SEED": seed if seed is not None else random.randint(0, 2**32),
        "PARAM_INT_STEPS": steps,
        "PARAM_FLOAT_CFG": cfg,
    }
    filled = _fill_params(wf, params)
    prompt_id = await comfyui_submit(filled)
    outputs = await comfyui_poll(prompt_id, timeout_s=1800)
    audio_path = _find_output_audio(outputs)
    if not audio_path:
        log.error(f"ComfyUI song: no audio output found for {prompt_id}")
        return None
    return _copy_to_dest(audio_path, dest_dir, name)
