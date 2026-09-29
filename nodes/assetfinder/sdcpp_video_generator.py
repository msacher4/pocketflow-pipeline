import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from config import SDCPP_FRAMES, SDCPP_FPS, SDCPP_STEPS, SDCPP_TIMEOUT
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.sdcpp_api import sdcpp_generate_video_t2v

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"

FPS = float(SDCPP_FPS)


class SDCppVideoGenerator(AsyncNode):
    """Génère des clips vidéo T2V via stable-diffusion.cpp (LTX-2.5 22B distill) pour chaque slot visuel."""

    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "sdcpp_video_gen"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        slots = blueprint.get("slots", [])
        if not slots:
            raise RuntimeError("sdcpp_video_gen: no visual slots in blueprint")

        slots_to_regenerate = shared.get("_slots_to_regenerate", [])
        if slots_to_regenerate:
            slots = [s for s in slots if s.get("id") in slots_to_regenerate]
            log.info(f"SDCppVideoGenerator: partial regen for slots {slots_to_regenerate}")

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        generated = []
        feedback = shared.get("af_feedback", "")
        for slot in slots:
            # Les slots I2V (image réelle, run alt) sont générés par SDCppI2VNode,
            # pas en T2V ici.
            if slot.get("mode") == "i2v":
                log.info(f"SDCppVideoGenerator: slot {slot.get('id')} est I2V, ignoré en T2V")
                continue
            slot_id = slot.get("id", "unknown")
            prompt = slot.get("prompt", "")
            if not prompt:
                log.warning(f"Slot {slot_id}: empty prompt, skipping T2V")
                continue
            if feedback:
                prompt = f"{prompt}. Avoid: {feedback}"

            # Le prompt part TEL QUEL (recette validée : les tests ont montré que le
            # suffixe générique "energetic motion..." écrasait le sujet au lieu
            # d'aider). Le mouvement vient du prompt lui-même (règles des souls).

            # Négatif combiné : le vrai negative_prompt du LLM (s'il existe) +
            # les keywords de la doc officielle LTX2.5 (toujours présents).
            from helpers.sdcpp_api import SDCPP_DEFAULT_NEGATIVE
            from helpers.headcount_guard import classify_headcount, apply_headcount_guard
            neg_llm = (slot.get("negative_prompt") or "").strip().rstrip(",").strip()
            negative = f"{neg_llm}, {SDCPP_DEFAULT_NEGATIVE}" if neg_llm else None

            # Garde-fou headcount (JEV) : décide si le prompt décrit 1 personne,
            # 2 personnes ou une foule, puis verrouille prompt + négatif en
            # conséquence (seul → alone + anti-figurants ; jamais sur doute/échec).
            verdict = await classify_headcount(prompt)
            prompt, negative, meta = apply_headcount_guard(prompt, negative, verdict)
            log.info(f"Slot {slot_id}: headcount_guard -> {meta}")

            is_regen = bool(shared.get("_slots_to_regenerate"))
            steps = 16 if is_regen else SDCPP_STEPS
            timeout_s = 2400 if is_regen else SDCPP_TIMEOUT

            log.info(f"Generating T2V video for slot {slot_id} (steps={steps}): {prompt[:80]}...")
            path = await sdcpp_generate_video_t2v(
                prompt=prompt,
                dest_dir=dest,
                name=f"clip_{slot_id}_raw",
                steps=steps,
                timeout_s=timeout_s,
                negative=negative,
            )
            if not path:
                log.warning(f"Slot {slot_id}: sd-cli failed (killed/OOM?), skipping")
                continue
            generated.append({
                "slot_id": slot_id,
                "section": slot.get("section", ""),
                "position": slot.get("position", 0),
                "plan_index": slot.get("plan_index"),
                "content": slot.get("content", ""),
                "expected": slot.get("expected", ""),
                "video_path": path,
                "duration_s": SDCPP_FRAMES / FPS,
            })

        if not generated:
            raise RuntimeError("sdcpp_video_gen: no videos generated")

        log.info(f"SDCppVideoGenerator -> {len(generated)}/{len(slots)} videos generated")
        return json.dumps({"generated_videos": generated}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("SDCppVideoGenerator POST -> exec is not valid JSON")
            shared["_current_step"] = "sdcpp_video_gen_error"
            shared["_error"] = "SDCppVideoGenerator: exec is not valid JSON"
            shared["steps"].append({
                "step": "sdcpp_video_gen", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("SDCppVideoGenerator aborted: exec is not valid JSON")

        new_videos = data.get("generated_videos", [])
        partial = shared.get("_slots_to_regenerate")
        if partial:
            prev = shared.get("generated_videos", [])
            new_ids = {v.get("slot_id") for v in new_videos}
            kept = [v for v in prev if v.get("slot_id") not in new_ids]
            shared["generated_videos"] = kept + new_videos
            log.info(f"SDCppVideoGenerator: merged {len(new_videos)} regenerated + {len(kept)} kept")
        else:
            shared["generated_videos"] = new_videos
        shared["_current_step"] = "sdcpp_video_gen_done"
        shared["steps"].append({
            "step": "sdcpp_video_gen", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(shared['generated_videos'])} videos generated",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        if shared.get("_slots_to_regenerate"):
            return "regen_done"
        return "default"
