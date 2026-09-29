import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from config import SDCPP_I2V_HEIGHT, SDCPP_I2V_WIDTH
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.comfyui_api import comfyui_generate_image, comfyui_ensure_started

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"


class ComfyUIImageGenerator(AsyncNode):
    """Génère des images via Klein (FLUX) pour chaque slot visuel du blueprint.

    Résolution = cible i2v (SDCPP_I2V_WIDTH×SDCPP_I2V_HEIGHT) : l'image est
    générée directement en 540×960 portrait, sans crop central côté sd-cli.
    """

    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "comfyui_image_gen"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        slots = blueprint.get("slots", [])
        if not slots:
            raise RuntimeError("comfyui_image_gen: no visual slots in blueprint")

        slots_to_regenerate = shared.get("_slots_to_regenerate", [])
        if slots_to_regenerate:
            slots = [s for s in slots if s.get("id") in slots_to_regenerate]
            log.info(f"ComfyUIImageGenerator: partial regen for slots {slots_to_regenerate}")

        # Prompt image fourni par l'utilisateur en mode i2v (régénération)
        user_image_prompt = shared.get("_i2v_image_prompt", "")

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        await comfyui_ensure_started()
        log.info("ComfyUIImageGenerator: ComfyUI ensured started")

        generated = []
        for slot in slots:
            slot_id = slot.get("id", "unknown")
            prompt = user_image_prompt if user_image_prompt and slot_id in slots_to_regenerate else slot.get("prompt", "")
            negative = slot.get("negative_prompt", "blurry, low quality, text, watermark")
            if not prompt:
                log.warning(f"Slot {slot_id}: empty prompt, skipping")
                continue

            log.info(f"Generating image for slot {slot_id}: {prompt[:80]}...")
            path = await comfyui_generate_image(
                prompt=prompt,
                negative_prompt=negative,
                dest_dir=dest,
                name=f"img_{slot_id}",
                width=SDCPP_I2V_WIDTH,
                height=SDCPP_I2V_HEIGHT,
            )
            generated.append({
                "slot_id": slot_id,
                "section": slot.get("section", ""),
                "position": slot.get("position", 0),
                "content": slot.get("content", ""),
                "prompt": prompt,
                "expected": slot.get("expected", ""),
                "image_path": path,
                "confirmed": False,
            })

        if not generated:
            raise RuntimeError("comfyui_image_gen: no images generated")

        log.info(f"ComfyUIImageGenerator -> {len(generated)}/{len(slots)} images generated")
        return json.dumps({"generated_images": generated}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("ComfyUIImageGenerator POST -> exec is not valid JSON")
            shared["_current_step"] = "comfyui_image_gen_error"
            shared["_error"] = "ComfyUIImageGenerator: exec is not valid JSON"
            shared["steps"].append({
                "step": "comfyui_image_gen", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ComfyUIImageGenerator aborted: exec is not valid JSON")

        shared["generated_images"] = data.get("generated_images", [])
        partial = shared.get("_slots_to_regenerate")
        if partial:
            prev = shared.get("generated_images", [])
            new_ids = {img.get("slot_id") for img in shared["generated_images"]}
            kept = [img for img in prev if img.get("slot_id") not in new_ids]
            shared["generated_images"] = kept + shared["generated_images"]
            log.info(f"ComfyUIImageGenerator: merged {len(data.get('generated_images', []))} regen + {len(kept)} kept")
        shared["_current_step"] = "comfyui_image_gen_done"
        shared["steps"].append({
            "step": "comfyui_image_gen", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(shared['generated_images'])} images generated",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
