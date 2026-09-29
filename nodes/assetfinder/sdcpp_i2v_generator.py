import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from config import SDCPP_I2V_FRAMES, SDCPP_TIMEOUT
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.sdcpp_api import sdcpp_generate_video_i2v

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"

FPS = 16.0


def _find_image_for_slot(shared: dict, slot_id) -> str | None:
    """Retrouve le chemin de l'image validée pour un slot dans generated_images.

    Priorité : image marquée 'confirmed' (validée en phase 1), puis la plus récente
    du slot.
    """
    images = shared.get("generated_images", [])
    candidates = [i for i in images if i.get("slot_id") == slot_id]
    if not candidates:
        return None
    for img in candidates:
        if img.get("confirmed"):
            return img.get("image_path")
    return candidates[-1].get("image_path") if candidates[-1].get("image_path") else None


class SDCppI2VNode(AsyncNode):
    """Génère des clips vidéo I2V via stable-diffusion.cpp (LTX-2.5 22B distill).

    Prend l'image statique validée (phase 1) d'un slot et la transforme en vidéo
    animée (--strength 1.0, frame 0 préservée). Même build que le T2V (build_ltx25).
    Remplace l'ancienne boucle ControlNet (SAM2 + inpaint) : la correction se fait
    désormais en régénérant via i2v.
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "sdcpp_i2v_gen"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        slots_to_regenerate = shared.get("_slots_to_regenerate", [])
        if not slots_to_regenerate:
            raise RuntimeError("sdcpp_i2v_gen: no slots to regenerate")

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        blueprint = shared.get("asset_blueprint", {})
        all_slots = {s.get("id"): s for s in blueprint.get("slots", [])}

        feedback = shared.get("af_feedback", "")

        generated = []
        for slot_id in slots_to_regenerate:
            image_path = _find_image_for_slot(shared, slot_id)
            if not image_path:
                log.warning(f"Slot {slot_id}: no validated image, skipping I2V")
                continue

            slot = all_slots.get(slot_id, {})
            prompt = slot.get("prompt", "")
            if not prompt:
                log.warning(f"Slot {slot_id}: empty prompt, skipping I2V")
                continue
            if feedback:
                prompt = f"{prompt}. Avoid: {feedback}"
            # Forçage dynamisme (garantie code), même consigne que la génération I2V
            # initiale : animer le personnage et la caméra, jamais figer la frame.
            prompt = (
                f"{prompt}. Subtle idle animation, flowing hair and cloth in motion, "
                f"dynamic recognizable pose, gentle cinematic dolly-in, alive and in "
                f"motion, never static or frozen."
            )

            log.info(f"Generating I2V video for slot {slot_id}: {prompt[:80]}...")
            path = await sdcpp_generate_video_i2v(
                prompt=prompt,
                image_path=image_path,
                dest_dir=dest,
                name=f"clip_{slot_id}_raw_i2v",
                frames=SDCPP_I2V_FRAMES,
                timeout_s=SDCPP_TIMEOUT,
            )
            if not path:
                log.warning(f"Slot {slot_id}: sd-cli i2v failed (killed/OOM?), skipping")
                continue
            generated.append({
                "slot_id": slot_id,
                "section": slot.get("section", ""),
                "position": slot.get("position", 0),
                "plan_index": slot.get("plan_index"),
                "content": slot.get("content", ""),
                "expected": slot.get("expected", ""),
                "video_path": path,
                "image_path": image_path,
                "duration_s": SDCPP_I2V_FRAMES / FPS,
            })

        if not generated:
            raise RuntimeError("sdcpp_i2v_gen: no videos generated")

        log.info(f"SDCppI2VNode -> {len(generated)}/{len(slots_to_regenerate)} videos generated")
        return json.dumps({"generated_videos": generated}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("SDCppI2VNode POST -> exec is not valid JSON")
            shared["_current_step"] = "sdcpp_i2v_gen_error"
            shared["_error"] = "SDCppI2VNode: exec is not valid JSON"
            shared["steps"].append({
                "step": "sdcpp_i2v_gen", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("SDCppI2VNode aborted: exec is not valid JSON")

        new_videos = data.get("generated_videos", [])
        partial = shared.get("_slots_to_regenerate")
        if partial:
            prev = shared.get("generated_videos", [])
            new_ids = {v.get("slot_id") for v in new_videos}
            kept = [v for v in prev if v.get("slot_id") not in new_ids]
            shared["generated_videos"] = kept + new_videos
            log.info(f"SDCppI2VNode: merged {len(new_videos)} regenerated + {len(kept)} kept")
        else:
            shared["generated_videos"] = new_videos
        shared["_current_step"] = "sdcpp_i2v_gen_done"
        shared["steps"].append({
            "step": "sdcpp_i2v_gen", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(shared['generated_videos'])} videos generated via i2v",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "regen_done"
