import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from config import SDCPP_I2V_HEIGHT, SDCPP_I2V_WIDTH
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.comfyui_api import comfyui_ensure_started, comfyui_generate_image_ref

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"

# Les références viennent exclusivement de Danbooru (helpers/danbooru.py,
# rating:g), donc ce sont TOUJOURS des illustrations anime. Sans verrou
# explicite, Klein dérive vers un rendu gras/photoréaliste : le scaler
# 1 MP `nearest-exact` préserve les lignes nettes du trait anime, et cette
# clause fige le style. Si la source de référence change (photo réelle), cette
# clause doit devenir conditionnelle.
STYLE_SUFFIX = "anime illustration, clean lineart, cel shading, flat colors"


def _reference_paths_for_slot(shared, slot_id) -> list[str]:
    """Références Danbooru déjà téléchargées pour ce slot par
    RealCharacterImageNode, relues dans `generated_images`."""
    for img in shared.get("generated_images", []):
        if img.get("slot_id") != slot_id:
            continue
        refs = img.get("reference_paths") or []
        if isinstance(refs, str):
            refs = [refs]
        return [str(r) for r in refs if r]
    return []


class ComfyUIKleinRefImageGenerator(AsyncNode):
    """Génère l'image I2V de chaque slot avec Klein 4B, GUIDÉE PAR RÉFÉRENCE.

    Au lieu d'utiliser l'image Danbooru telle quelle comme frame de départ
    (souvent cadrée/posée pour le portrait et donc inutilisable en vidéo), on
    demande à Klein une image neuve : le prompt du slot décrit la scène, les
    images Danbooru ne fournissent que l'identité du personnage.

    Mécanisme ComfyUI : nœud natif `ReferenceLatent`, qui injecte les latents
    encodés des références dans le conditioning (positif ET négatif). Le canvas
    reste vide et `denoise=1.0`, donc la composition suit le prompt et non la
    photo de référence.

    Résolution = cible i2v (SDCPP_I2V_WIDTH×SDCPP_I2V_HEIGHT), soit exactement
    la passe BASE de l'I2V, qui fait ensuite un hires latent x2 vers 640×1152.
    """

    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "comfyui_klein_ref_gen"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        slots = [s for s in blueprint.get("slots", []) if s.get("mode") == "i2v"]
        if not slots:
            raise RuntimeError("comfyui_klein_ref_gen: no i2v slots in blueprint")

        regen = set(shared.get("_i2v_regen_image_slots") or []) | set(shared.get("_slots_to_regenerate") or [])
        if regen:
            slots = [s for s in slots if s.get("id") in regen]
            log.info(f"ComfyUIKleinRefImageGenerator: partial regen for slots {sorted(regen)}")
        if not slots:
            return json.dumps({"generated_images": []}, ensure_ascii=False)

        # Prompt image fourni par l'utilisateur en régénération.
        user_image_prompt = shared.get("_i2v_image_prompt", "")

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        await comfyui_ensure_started()
        log.info("ComfyUIKleinRefImageGenerator: ComfyUI ensured started")

        generated = []
        for slot in slots:
            slot_id = slot.get("id", "unknown")
            refs = _reference_paths_for_slot(shared, slot_id)
            if not refs:
                log.error(f"Slot {slot_id}: aucune image de référence Danbooru -> Klein ignoré")
                continue

            prompt = user_image_prompt if user_image_prompt and slot_id in regen else slot.get("prompt", "")
            negative = slot.get("negative_prompt", "blurry, low quality, text, watermark")
            if not prompt:
                log.warning(f"Slot {slot_id}: empty prompt, skipping")
                continue

            prompt = f"{prompt}, {STYLE_SUFFIX}"

            log.info(f"Generating image for slot {slot_id} from {len(refs)} ref(s): {prompt[:80]}...")
            path = await comfyui_generate_image_ref(
                prompt=prompt,
                negative_prompt=negative,
                reference_images=refs,
                dest_dir=dest,
                name=f"klein_{slot_id}",
                width=SDCPP_I2V_WIDTH,
                height=SDCPP_I2V_HEIGHT,
            )
            if not path:
                log.error(f"Slot {slot_id}: Klein n'a rien retourné")
                continue
            generated.append({
                "slot_id": slot_id,
                "section": slot.get("section", ""),
                "position": slot.get("position", 0),
                "content": slot.get("content", ""),
                "prompt": prompt,
                "expected": slot.get("expected", ""),
                "image_path": path,
                "reference_paths": refs,
                "confirmed": False,
            })

        if not generated:
            raise RuntimeError("comfyui_klein_ref_gen: no images generated")

        log.info(f"ComfyUIKleinRefImageGenerator -> {len(generated)}/{len(slots)} images generated")
        return json.dumps({"generated_images": generated}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("ComfyUIKleinRefImageGenerator POST -> exec is not valid JSON")
            shared["_current_step"] = "comfyui_klein_ref_gen_error"
            shared["_error"] = "ComfyUIKleinRefImageGenerator: exec is not valid JSON"
            shared["steps"].append({
                "step": "comfyui_klein_ref_gen", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ComfyUIKleinRefImageGenerator aborted: exec is not valid JSON")

        new_images = data.get("generated_images", [])
        new_ids = {img.get("slot_id") for img in new_images}
        # REMPLACEMENT, pas ajout : _find_image_for_slot privilégie la première
        # entrée `confirmed`. Si l'image Danbooru (confirmed=True) survivait,
        # l'I2V continuerait de l'utiliser au lieu de l'image Klein.
        prev = shared.get("generated_images", [])
        kept = [img for img in prev if img.get("slot_id") not in new_ids]
        shared["generated_images"] = kept + new_images

        shared["_current_step"] = "comfyui_klein_ref_gen_done"
        shared["steps"].append({
            "step": "comfyui_klein_ref_gen", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(new_images)} image(s) Klein (réf. Danbooru), {len(kept)} conservée(s)",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"