import asyncio
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"
COMFYUI_WORKFLOWS_DIR = Path(__file__).parent.parent.parent / "workflows"


def _find_video_for_slot(shared: dict, slot_id) -> str | None:
    """Find the video path for a given slot_id in generated_videos."""
    for v in shared.get("generated_videos", []):
        if v.get("slot_id") == slot_id:
            return v.get("video_path")
    return None


def _get_slot_prompt(shared: dict, slot_id) -> str:
    """Get the original prompt for a slot from the blueprint."""
    blueprint = shared.get("asset_blueprint", {})
    for s in blueprint.get("slots", []):
        if s.get("id") == slot_id:
            return s.get("prompt", "")
    return ""


def _extract_frames(video_path: str, dest_dir: Path, fps: float = 8.0) -> Path | None:
    """Extract frames from video using ffmpeg. Returns directory containing frames."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-i", video_path,
                "-vf", f"fps={fps},scale=-1:1280",
                "-q:v", "2",
                str(dest_dir / "frame_%04d.png"),
            ],
            capture_output=True, timeout=60,
        )
        frame_count = len(list(dest_dir.glob("frame_*.png")))
        if frame_count == 0:
            log.error(f"ControlNet: no frames extracted from {video_path}")
            return None
        log.info(f"ControlNet: extracted {frame_count} frames from {video_path}")
        return dest_dir
    except Exception as e:
        log.error(f"ControlNet: frame extraction failed: {e}")
        return None


_DEFECT_EN = {
    "bulle": "bubbles",
    "bulles": "bubbles",
    "mousse": "foam",
    "ecume": "froth",
    "écume": "froth",
    "froth": "froth",
    "foam": "foam",
    "bubbles": "bubbles",
    "flou": "blur",
    "reflet": "reflections",
    "reflets": "reflections",
    "bruit": "noise",
    "artefact": "artifacts",
    "artefacts": "artifacts",
    "artifact": "artifacts",
    "artifacts": "artifacts",
}

_STOP_WORDS = {
    "de", "des", "du", "la", "le", "les", "un", "une", "trop", "très", "tres",
    "beaucoup", "pas", "plus", "the", "a", "an", "with", "dégueulasses",
    "dégueulasse", "dégueulase", "moche", "horrible", "laid", "laisse",
    "genant", "gênant", "cas", "je", "tu", "il", "elle", "on", "nous", "vous",
    "ils", "elles", "que", "qui", "quoi", "dans", "sur", "et", "ou", "a", "au",
    "aux", "ne", "moi", "toi", "c'est", "cet", "cette", "ces", "y", "en", "se",
    "sa", "son", "ses", "mes", "tes", "sans",
}

_OBJECT_EN = {
    "smoothie": "smoothie",
    "verre": "glass",
    "boisson": "drink",
    "jus": "juice",
    "cafe": "coffee",
    "café": "coffee",
    "the": "tea",
    "thé": "tea",
    "lait": "milk",
    "eau": "water",
    "biere": "beer",
    "bière": "beer",
    "vin": "wine",
    "cocktail": "cocktail",
    "milkshake": "milkshake",
    "frappe": "frappe",
    "latte": "latte",
    "cappuccino": "cappuccino",
    "espresso": "espresso",
    "cola": "cola",
    "soda": "soda",
    "limonade": "lemonade",
    "orange": "orange",
    "fraise": "strawberry",
    "framboise": "raspberry",
    "bleuet": "blueberry",
    "myrtille": "blueberry",
    "mangue": "mango",
    "peche": "peach",
    "pêche": "peach",
    "citron": "lemon",
    "pomme": "apple",
    "banane": "banana",
    "kiwi": "kiwi",
    "ananas": "pineapple",
    "pasteque": "watermelon",
    "pastèque": "watermelon",
    "melon": "melon",
    "raisin": "grape",
    "cereale": "cereal",
    "céréale": "cereal",
    "bol": "bowl",
    "tasse": "cup",
    "verre": "glass",
    "bouteille": "bottle",
    "canette": "can",
    "gobelet": "cup",
}


def _extract_defect_terms(feedback: str) -> list[str]:
    """Extract meaningful defect terms from the avoid-list, translated to English."""
    if not feedback:
        return []
    terms: set[str] = set()
    for chunk in re.split(r"[,;.!?]|\bet\b|\band\b", feedback.lower()):
        chunk = chunk.strip().strip("()[]{}'\"")
        if not chunk:
            continue
        if chunk in _DEFECT_EN:
            terms.add(_DEFECT_EN[chunk])
            continue
        for word in chunk.split():
            word = word.strip("()[]{}'\"").rstrip("s")
            if word and word not in _STOP_WORDS and word in _DEFECT_EN:
                terms.add(_DEFECT_EN[word])
    return sorted(terms)


def _build_detect_prompt(user_target: str, feedback: str, fallback: str) -> str:
    """Build the SAM2 detect prompt: target the subject to mask for inpainting.

    GroundingDINO works best with simple object names (glass, hand, person, cup).
    Always append generic detectable terms to avoid empty detections that break SAM2.
    """
    defects = _extract_defect_terms(feedback)
    if defects:
        seg = [d for d in defects if d in ("bubbles", "foam", "froth")]
        if seg:
            return "glass, hand, green liquid"
        return ", ".join(defects)
    # Translate French object terms to English for GroundingDINO
    if user_target:
        words = user_target.lower().split()
        en_words = [_OBJECT_EN.get(w, w) for w in words]
        base = " ".join(en_words)
    elif fallback:
        words = fallback.lower().split()
        en_words = [_OBJECT_EN.get(w, w) for w in words]
        base = " ".join(en_words)
    else:
        base = "glass, hand"
    # Always append generic detectable objects to prevent SAM2 break on empty detection
    if "glass" not in base and "cup" not in base and "hand" not in base:
        base = f"{base}, glass, hand"
    return base


def _build_correction_prompt(base_prompt: str, feedback: str) -> str:
    """Build corrected prompt: explicitly negate the avoid-list defects."""
    defects = _extract_defect_terms(feedback)
    if not defects:
        return f"{base_prompt}, smooth, natural texture, no artifacts"
    no_list = ", ".join(f"no {d}" for d in defects)
    return f"{base_prompt}, {no_list}, smooth, clean, natural texture, no artifacts"


class VideoControlNetNode(AsyncNode):
    """Corrige un clip vidéo via SAM2 + SDXL Inpaint + LoRA Hyper-SD.

    Ce node:
    1. Extrait les frames du clip rejeté
    2. Lance le workflow ComfyUI de correction
    3. Remplace le clip dans le shared state
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "video_controlnet"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        slots_to_regenerate = shared.get("_slots_to_regenerate", [])
        if not slots_to_regenerate:
            raise RuntimeError("video_controlnet: no slots to regenerate")

        slot_id = slots_to_regenerate[0]
        feedback = shared.get("af_feedback", "")

        video_path = _find_video_for_slot(shared, slot_id)
        if not video_path or not Path(video_path).is_file():
            raise RuntimeError(f"video_controlnet: video not found for slot {slot_id}")

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        frames_dir = dest / f"controlnet_frames_{slot_id}"
        extracted = _extract_frames(video_path, frames_dir)
        if not extracted:
            raise RuntimeError(f"video_controlnet: frame extraction failed for {video_path}")

        original_prompt = _get_slot_prompt(shared, slot_id)
        user_target = shared.get("_cn_detect_target", "")
        detect_prompt = _build_detect_prompt(user_target, feedback, original_prompt)
        corrected_prompt = _build_correction_prompt(original_prompt, feedback)

        log.info(f"ControlNet: processing slot {slot_id}, prompt: {corrected_prompt[:80]}..., "
                 f"detect: {detect_prompt[:60]}")

        from helpers.comfyui_controlnet import run_controlnet_workflow, DEFAULT_NEGATIVE_PROMPT
        output_path = await run_controlnet_workflow(
            frames_dir=str(extracted),
            prompt=corrected_prompt,
            dest_dir=dest,
            name=f"clip_{slot_id}_controlnet",
            detect_prompt=detect_prompt,
            negative_prompt=(f"{DEFAULT_NEGATIVE_PROMPT}, {feedback}" if feedback else DEFAULT_NEGATIVE_PROMPT),
            fps=8,
        )

        if not output_path:
            raise RuntimeError(f"video_controlnet: ComfyUI workflow failed for slot {slot_id}")

        log.info(f"ControlNet: corrected video -> {output_path}")
        return json.dumps({
            "corrected_videos": [{
                "slot_id": slot_id,
                "video_path": output_path,
            }],
        }, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("VideoControlNet POST -> exec is not valid JSON")
            shared["_current_step"] = "video_controlnet_error"
            shared["_error"] = "VideoControlNet: exec is not valid JSON"
            shared["steps"].append({
                "step": "video_controlnet", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VideoControlNet aborted: exec is not valid JSON")

        corrected = data.get("corrected_videos", [])
        partial = shared.get("_slots_to_regenerate")
        if partial and corrected:
            prev = shared.get("generated_videos", [])
            new_ids = {v.get("slot_id") for v in corrected}
            kept = [v for v in prev if v.get("slot_id") not in new_ids]
            shared["generated_videos"] = kept + corrected
            log.info(f"VideoControlNet: merged {len(corrected)} corrected + {len(kept)} kept")
        else:
            log.warning(f"VideoControlNet: no corrected videos returned")

        shared["_current_step"] = "video_controlnet_done"
        shared["steps"].append({
            "step": "video_controlnet", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(corrected)} videos corrected via ControlNet",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "regen_done"
