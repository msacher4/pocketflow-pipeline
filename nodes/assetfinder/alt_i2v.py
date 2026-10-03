import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from config import SDCPP_FPS, SDCPP_I2V_FRAMES, SDCPP_TIMEOUT
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.sdcpp_api import sdcpp_generate_video_i2v
from nodes.assetfinder.sdcpp_i2v_generator import _find_image_for_slot

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"
# Doit venir de SDCPP_FPS (24), comme le T2V. La valeur 16.0 codee en dur
# faisait annoncer 97/16 = 6.06 s pour un clip qui dure 97/24 = 4.04 s. Cette
# duree est reprise par montage_planner.py (end_s=duration_s), donc chaque clip
# I2V se faisait etirer de 2 s sur la timeline, avec 2 s de trou noir.
FPS = float(SDCPP_FPS)


class AltI2VNode(AsyncNode):
    """Génère les clips I2V du run alt à partir des images réelles des personnages.

    Réutilise la génération I2V de stable-diffusion.cpp (même API que SDCppI2VNode).
    Pour chaque slot du blueprint marqué `mode == "i2v"` :
    - récupère l'image réelle trouvée par RealCharacterImageNode (generated_images),
    - génère la vidéo I2V (frame 0 = l'image).
    En cas d'échec d'un slot : on retire `mode` (la vidéo pourra être refaite en
    T2V par la boucle de feedback existante). Ne bloque pas sur les échecs partiels.
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "alt_i2v_gen"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        slots = blueprint.get("slots", [])
        i2v_slots = [s for s in slots if s.get("mode") == "i2v"]
        if not i2v_slots:
            return json.dumps({"generated_videos": [], "failed_ids": []}, ensure_ascii=False)

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        log.info(f"AltI2VNode: génération I2V pour {len(i2v_slots)} slot(s)")
        generated = []
        failed_ids = []
        for slot in i2v_slots:
            slot_id = slot.get("id")
            image_path = _find_image_for_slot(shared, slot_id)
            prompt = slot.get("prompt", "")
            if not image_path or not prompt:
                log.warning(f"AltI2VNode: slot {slot_id} sans image/prompt, fallback T2V")
                failed_ids.append(slot_id)
                continue
            # Forçage dynamisme (garantie code), adapté aux portraits I2V : on anime le
            # personnage (idle animation, cheveux/tenue, pose dynamique) avec un léger
            # mouvement caméra, sans jamais figer la frame.
            prompt = (
                f"{prompt}. Subtle idle animation, flowing hair and cloth in motion, "
                f"dynamic recognizable pose, gentle cinematic dolly-in, alive and in "
                f"motion, never static or frozen."
            )
            path = await sdcpp_generate_video_i2v(
                prompt=prompt,
                image_path=image_path,
                dest_dir=dest,
                name=f"clip_{slot_id}_raw_i2v",
                frames=SDCPP_I2V_FRAMES,
                timeout_s=SDCPP_TIMEOUT,
            )
            if not path:
                log.warning(f"AltI2VNode: échec I2V slot {slot_id}, fallback T2V")
                failed_ids.append(slot_id)
                continue
            generated.append({
                "slot_id": slot_id,
                "section": slot.get("section", ""),
                "position": slot.get("position", 0),
                "content": slot.get("content", ""),
                "expected": slot.get("expected", ""),
                "video_path": path,
                "image_path": image_path,
                "duration_s": SDCPP_I2V_FRAMES / FPS,
            })

        return json.dumps({"generated_videos": generated, "failed_ids": failed_ids},
                          ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("AltI2VNode POST -> exec is not valid JSON")
            shared["_current_step"] = "alt_i2v_gen_error"
            shared["_error"] = "AltI2VNode: exec is not valid JSON"
            shared["steps"].append({
                "step": "alt_i2v_gen", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("AltI2VNode aborted: exec is not valid JSON")

        new_videos = data.get("generated_videos", [])
        failed_ids = set(data.get("failed_ids", []))

        # Merge dans generated_videos (remplace les éventuelles vidéos du même slot).
        prev = shared.get("generated_videos", [])
        new_ids = {v.get("slot_id") for v in new_videos}
        kept = [v for v in prev if v.get("slot_id") not in new_ids]
        shared["generated_videos"] = kept + new_videos

        # Les slots i2v ayant échoué retombent en T2V (retirés du mode i2v).
        failed = 0
        for s in shared.get("asset_blueprint", {}).get("slots", []):
            if s.get("mode") == "i2v" and s.get("id") in failed_ids:
                s.pop("mode", None)
                failed += 1

        shared["_current_step"] = "alt_i2v_gen_done"
        shared["steps"].append({
            "step": "alt_i2v_gen", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(new_videos)} vidéo(s) I2V + {failed} fallback(s) T2V",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        if failed_ids:
            # Les slots ayant échoué en I2V doivent être régénérés en T2V par un
            # second passage de SDCppVideoGenerator (_slots_to_regenerate).
            shared["_slots_to_regenerate"] = list(failed_ids)
            return "regen"
        return "default"
