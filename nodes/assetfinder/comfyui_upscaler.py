import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from config import SDCPP_SKIP_UPSCALE
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.comfyui_api import comfyui_rife_interp
from nodes.assetfinder.remap_montage import _remap_montage_paths

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"


def _passthrough(video_path: str, dest_dir: Path, slot_id: str) -> str:
    """Renomme clip_<id>_raw.webm -> clip_<id>.webm sans upscale ComfyUI."""
    src = Path(video_path)
    if not src.is_file():
        log.warning(f"Slot {slot_id}: raw video missing ({src}), keeping original path")
        return video_path
    dest = dest_dir / f"clip_{slot_id}.webm"
    if src != dest:
        shutil.move(str(src), str(dest))
        log.info(f"Slot {slot_id}: 720p natif, pas d'upscale -> {dest.name}")
    else:
        log.info(f"Slot {slot_id}: 720p natif, fichier déjà final -> {dest.name}")
    return str(dest)


class ComfyUIUpscaler(AsyncNode):
    """Interpolation RIFE ×2 FPS via ComfyUI (sans upscale spatial RealESRGAN).

    En mode 720p natif (SDCPP_SKIP_UPSCALE=1) : simple pass-through
    qui renomme clip_<id>_raw.webm -> clip_<id>.webm sans passer par ComfyUI."""

    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "comfyui_upscale"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        videos = shared.get("generated_videos", [])
        if not videos:
            raise RuntimeError("comfyui_upscale: no generated videos")

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        upscaled = []
        for vid in videos:
            slot_id = vid.get("slot_id", "unknown")
            video_path = vid.get("video_path", "")

            if not video_path:
                log.warning(f"Slot {slot_id}: no video_path, skipping upscale")
                continue

            if SDCPP_SKIP_UPSCALE:
                path = _passthrough(video_path, dest, slot_id)
            else:
                log.info(f"RIFE interpolation for slot {slot_id}")
                path = await comfyui_rife_interp(
                    video_path=video_path,
                    dest_dir=dest,
                    name=f"clip_{slot_id}",
                )
            upscaled.append({
                "slot_id": slot_id,
                "section": vid.get("section", ""),
                "position": vid.get("position", 0),
                "content": vid.get("content", ""),
                "expected": vid.get("expected", ""),
                "video_path": path or video_path,
                "image_path": vid.get("image_path", ""),
                "duration_s": vid.get("duration_s", 97.0 / 25.0),
            })

        if not upscaled:
            raise RuntimeError("comfyui_upscale: no videos upscaled")

        mode = "720p natif (skip)" if SDCPP_SKIP_UPSCALE else "RIFE ×2"
        log.info(f"ComfyUIUpscaler -> {len(upscaled)}/{len(videos)} videos ({mode})")
        return json.dumps({"upscaled_videos": upscaled}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("ComfyUIUpscaler POST -> exec is not valid JSON")
            shared["_current_step"] = "comfyui_upscale_error"
            shared["_error"] = "ComfyUIUpscaler: exec is not valid JSON"
            shared["steps"].append({
                "step": "comfyui_upscale", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ComfyUIUpscaler aborted: exec is not valid JSON")

        shared["generated_videos"] = data.get("upscaled_videos", [])
        _remap_montage_paths(shared, data.get("upscaled_videos", []))
        shared["_current_step"] = "comfyui_upscale_done"
        shared["steps"].append({
            "step": "comfyui_upscale", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(shared['generated_videos'])} videos upscaled",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
