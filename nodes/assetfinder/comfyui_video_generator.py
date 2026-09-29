import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.comfyui_api import comfyui_generate_video_i2v

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"


class ComfyUIVideoGenerator(AsyncNode):
    """Génère des clips vidéo via LTX-2.3 22B à partir des images confirmées."""

    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "comfyui_video_gen"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        confirmed = shared.get("confirmed_images", []) or shared.get("generated_images", [])
        if not confirmed:
            raise RuntimeError("comfyui_video_gen: no confirmed images")

        blueprint = shared.get("asset_blueprint", {})
        all_slots = {s["id"]: s for s in blueprint.get("slots", [])}

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        generated = []
        for img in confirmed:
            slot_id = img.get("slot_id", "unknown")
            slot = all_slots.get(slot_id, {})
            prompt = slot.get("prompt", img.get("prompt", ""))
            negative = slot.get("negative_prompt", "blurry, low quality, text, watermark")
            image_path = img.get("image_path", "")

            if not image_path:
                log.warning(f"Slot {slot_id}: no image_path, skipping I2V")
                continue

            log.info(f"Generating video for slot {slot_id} (I2V from image)")
            path = await comfyui_generate_video_i2v(
                prompt=prompt,
                negative_prompt=negative,
                start_image=image_path,
                dest_dir=dest,
                name=f"clip_{slot_id}_raw",
            )
            generated.append({
                "slot_id": slot_id,
                "section": img.get("section", ""),
                "position": img.get("position", 0),
                "content": img.get("content", ""),
                "expected": img.get("expected", ""),
                "video_path": path,
                "image_path": image_path,
                "duration_s": 97.0 / 25.0,
            })

        if not generated:
            raise RuntimeError("comfyui_video_gen: no videos generated")

        log.info(f"ComfyUIVideoGenerator -> {len(generated)}/{len(confirmed)} videos generated")
        return json.dumps({"generated_videos": generated}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("ComfyUIVideoGenerator POST -> exec is not valid JSON")
            shared["_current_step"] = "comfyui_video_gen_error"
            shared["_error"] = "ComfyUIVideoGenerator: exec is not valid JSON"
            shared["steps"].append({
                "step": "comfyui_video_gen", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ComfyUIVideoGenerator aborted: exec is not valid JSON")

        shared["generated_videos"] = data.get("generated_videos", [])
        shared["_current_step"] = "comfyui_video_gen_done"
        shared["steps"].append({
            "step": "comfyui_video_gen", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(shared['generated_videos'])} videos generated",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
