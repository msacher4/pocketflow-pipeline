import json
import logging
import random
import shutil
from pathlib import Path

from helpers.comfyui_api import (
    comfyui_ensure_started,
    comfyui_free,
    comfyui_submit,
    comfyui_poll,
    _find_output_video,
    _copy_to_dest,
    COMFYUI_INPUT,
)

log = logging.getLogger("pocketflow-pipeline")

WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "video_controlnet.json"


def _load_workflow() -> dict:
    if not WORKFLOW_PATH.is_file():
        raise FileNotFoundError(f"ControlNet workflow not found: {WORKFLOW_PATH}")
    return json.loads(WORKFLOW_PATH.read_text())


def _prepare_input_frames(frame_files: list[Path], input_name: str) -> str:
    """Copy ALL frames into a ComfyUI input dir, return absolute FRAME_DIR."""
    input_dir = COMFYUI_INPUT / input_name
    input_dir.mkdir(parents=True, exist_ok=True)
    for f in frame_files:
        shutil.copy2(f, input_dir / f.name)
    log.info(f"ControlNet: prepared {len(frame_files)} frames in {input_dir}")
    return str(input_dir)


def _apply_params(workflow: dict, params: dict, seed: int, fps: int) -> None:
    """Inject placeholders + typed values into a fresh workflow copy."""
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        for key, val in inputs.items():
            if isinstance(val, str) and val in params:
                inputs[key] = params[val]
        # Typed injections (ComfyUI API is strict on INT/FLOAT)
        if node.get("class_type") == "KSampler":
            inputs["seed"] = seed
        elif node.get("class_type") == "VHS_VideoCombine":
            inputs["frame_rate"] = float(fps)


DEFAULT_NEGATIVE_PROMPT = (
    "(deformed iris, deformed pupils, semi-realistic, cgi, 3d, render, sketch, cartoon, "
    "drawing, anime:1.4), text, close up, cropped, out of frame, worst quality, low quality, "
    "jpeg artifacts, ugly, duplicate, morbid, mutilated, extra fingers, mutated hands, "
    "poorly drawn hands, poorly drawn face, mutation, deformed, blurry, bad anatomy, "
    "bad proportions, extra limbs, cloned face, disfigured, gross proportions, malformed limbs, "
    "missing arms, missing legs, extra arms, extra legs, fused fingers, too many fingers, "
    "long neck, artifacts, flicker"
)


async def run_controlnet_workflow(
    frames_dir: str,
    prompt: str,
    dest_dir: Path,
    name: str,
    detect_prompt: str = "",
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT,
    fps: int = 8,
    timeout_s: int = 900,
) -> str | None:
    """Lance le workflow ComfyUI de correction vidéo sur toutes les frames d'un coup.

    Pipeline : SAM2+GroundingDino (masque du sujet) -> RealVisXL Inpaint
    + LoRA Hyper-SD -> clip corrigé.

    Retourne le chemin du fichier vidéo corrigé, ou None en cas d'échec.
    """
    await comfyui_ensure_started()
    await comfyui_free(unload_models=True, free_memory=True)

    input_name = f"cn_input_{name}"
    frame_files = sorted(Path(frames_dir).glob("frame_*.png"))
    if not frame_files:
        log.error("ControlNet: no frames to process")
        return None
    frame_dir = _prepare_input_frames(frame_files, input_name)

    seed = random.randint(0, 2**31 - 1)

    log.info(
        f"ControlNet: {len(frame_files)} frames @ {fps}fps, seed {seed}, "
        f"prompt: {prompt[:60]}..."
    )

    workflow = _load_workflow()
    params = {
        "PROMPT": prompt,
        "NEGATIVE_PROMPT": negative_prompt,
        "DETECT_PROMPT": detect_prompt or prompt,
        "FRAME_DIR": frame_dir,
        "OUTPUT_NAME": name,
    }
    _apply_params(workflow, params, seed, fps)

    try:
        prompt_id = await comfyui_submit(workflow)
        outputs = await comfyui_poll(prompt_id, timeout_s=timeout_s)
    except TimeoutError:
        log.error(f"ControlNet: workflow timeout after {timeout_s}s")
        return None
    except RuntimeError as e:
        log.error(f"ControlNet: workflow error: {e}")
        return None

    video_path = _find_output_video(outputs)
    await comfyui_free(unload_models=True, free_memory=True)
    if not video_path:
        log.error(f"ControlNet: no video in outputs: {list(outputs.keys())}")
        return None

    result = _copy_to_dest(video_path, dest_dir, name)
    log.info(f"ControlNet: done -> {result}")

    shutil.rmtree(COMFYUI_INPUT / input_name, ignore_errors=True)
    return result
