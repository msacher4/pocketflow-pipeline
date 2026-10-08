import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
from pocketflow import AsyncNode

from config import TG_BOT_TOKEN, TG_CHAT_ID, TG_VALIDATION_TIMEOUT
from helpers.state import _set_state, _shared_snapshot, _register_validation, _pending_validations
from helpers.send_telegram import send_telegram
from nodes.assetfinder.sdcpp_i2v_generator import _find_image_for_slot

log = logging.getLogger("pocketflow-pipeline")


async def send_photo_tg(path: str, caption: str, buttons: list | None = None) -> int | None:
    """Send a photo to Telegram via sendPhoto (compression auto si > 10 Mo)."""
    from helpers.state import is_auto_approve
    if is_auto_approve():
        log.info("[tg] auto-approve skip send_photo_tg")
        return None
    if not TG_BOT_TOKEN:
        return None
    import os
    from helpers.send_telegram import compress_image_for_telegram
    original = path
    path = await compress_image_for_telegram(path)
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            files = {"photo": ("image.jpg", open(path, "rb"), "image/jpeg")}
            payload = {"chat_id": TG_CHAT_ID, "caption": caption}
            if buttons:
                payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
            r = await c.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data=payload, files=files,
            )
            if r.status_code == 200:
                return r.json().get("result", {}).get("message_id")
            log.warning(f"sendPhoto failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        log.warning(f"send_photo_tg error: {e}")
    finally:
        if path != original and os.path.isfile(path):
            os.unlink(path)
    return None


class ValidateI2V(AsyncNode):
    """Validation Telegram du binôme image réelle + prompt I2V réécrit.

    S'exécute juste après RewriteI2VPromptNode. Pour chaque slot i2v du blueprint :
    - 1 message photo (image réellement sélectionnée),
    - 1 message texte (prompt I2V réécrit d'après cette image),
    chacun avec des boutons ✅ Approuver / ❌ Rejeter.
    Les rejets sont appliqués par retour dans le flow : broiimage_ou alors
    regen_image → real_character_image, regen_prompt → rewrite_i2v_prompt,
    à raison des seuls slots rejetés. Timeout → auto-approve.
    """

    step = "validate_i2v"

    def __init__(self, timeout: int = TG_VALIDATION_TIMEOUT):
        super().__init__(max_retries=1, wait=5)
        self.timeout = timeout

    async def prep_async(self, shared):
        shared["_current_step"] = "validate_i2v"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        slots = blueprint.get("slots", [])
        i2v_slots = [s for s in slots if s.get("mode") == "i2v"]
        if not i2v_slots:
            return json.dumps({"action": "approve", "rejected_images": [], "rejected_prompts": []}, ensure_ascii=False)

        regen_images = set(shared.get("_i2v_regen_image_slots", []) or [])
        regen_prompts = set(shared.get("_i2v_regen_prompt_slots", []) or [])
        if regen_images or regen_prompts:
            i2v_slots = [s for s in i2v_slots if s.get("id") in regen_images or s.get("id") in regen_prompts]
            if not i2v_slots:
                return json.dumps({"action": "approve", "rejected_images": [], "rejected_prompts": []}, ensure_ascii=False)

        pipeline_id = shared.get("pipeline_id", "unknown")

        image_vids = {}
        prompt_vids = {}
        for slot in i2v_slots:
            sid = slot.get("id")
            image_vids[sid] = f"{pipeline_id}_pf_i2v_img_{sid}"
            prompt_vids[sid] = f"{pipeline_id}_pf_i2v_prm_{sid}"

        sent_events = []
        for slot in i2v_slots:
            sid = slot.get("id")
            image_path = _find_image_for_slot(shared, sid)
            prompt = slot.get("prompt", "")

            if image_path:
                vid = image_vids[sid]
                caption = self._image_caption(slot)
                buttons = [
                    [
                        {"text": "✅ Image OK", "callback_data": f"approve:{vid}"},
                        {"text": "❌ Refaire l'image", "callback_data": f"reject:{vid}"},
                    ],
                ]
                msg_id = await send_photo_tg(image_path, caption, buttons)
                if msg_id:
                    sent_events.append((sid, "image", vid, _register_validation(vid)))
            else:
                log.warning(f"ValidateI2V: slot {sid} sans image, validation image ignorée")

            if prompt:
                vid = prompt_vids[sid]
                buttons = [
                    [
                        {"text": "✅ Prompt OK", "callback_data": f"approve:{vid}"},
                        {"text": "❌ Réécrire", "callback_data": f"reject:{vid}"},
                    ],
                ]
                msg_id = await send_telegram(
                    f"[{sid}] {slot.get('section', '')} — Prompt I2V réécrit :\n{prompt}",
                    buttons,
                )
                if msg_id:
                    sent_events.append((sid, "prompt", vid, _register_validation(vid)))
            else:
                log.warning(f"ValidateI2V: slot {sid} sans prompt réécrit, validation prompt ignorée")

        events = [e for _, _, _, e in sent_events]
        if events:
            try:
                await asyncio.wait_for(asyncio.gather(*(e.wait() for e in events)), timeout=self.timeout)
            except asyncio.TimeoutError:
                log.info("ValidateI2V timeout, auto-approve")
                for _, _, vid, _ in sent_events:
                    _pending_validations.pop(vid, None)
                return json.dumps({"action": "approve", "rejected_images": [], "rejected_prompts": []}, ensure_ascii=False)

        rejected_images = []
        rejected_prompts = []
        for sid, kind, vid, _ in sent_events:
            entry = _pending_validations.pop(vid, {})
            result = entry.get("result", "reject")
            if not result.startswith("approve"):
                if kind == "image":
                    rejected_images.append(sid)
                else:
                    rejected_prompts.append(sid)

        log.info(f"ValidateI2V: rejected_images={rejected_images} rejected_prompts={rejected_prompts}")

        if not rejected_images and not rejected_prompts:
            return json.dumps({"action": "approve", "rejected_images": [], "rejected_prompts": []}, ensure_ascii=False)

        # Une image rejetée implique aussi un nouveau prompt sur ce slot.
        for sid in rejected_images:
            if sid not in rejected_prompts:
                rejected_prompts.append(sid)

        shared["_i2v_regen_image_slots"] = rejected_images
        shared["_i2v_regen_prompt_slots"] = rejected_prompts

        if rejected_images:
            return json.dumps({"action": "regen_image", "rejected_images": rejected_images, "rejected_prompts": rejected_prompts}, ensure_ascii=False)
        return json.dumps({"action": "regen_prompt", "rejected_images": rejected_images, "rejected_prompts": rejected_prompts}, ensure_ascii=False)

    def _image_caption(self, slot) -> str:
        sid = slot.get("id")
        section = slot.get("section", "")
        position = slot.get("position", 0)
        return f"[{sid}] {section} (position {position}) — image réelle\nVisuel attendu : {slot.get('content') or slot.get('prompt', '')[:80]}"

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            data = {"action": "reject"}

        action = data.get("action", "reject")
        rejected_images = data.get("rejected_images", [])
        rejected_prompts = data.get("rejected_prompts", [])

        shared["steps"].append({
            "step": "validate_i2v", "status": action,
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"img={len(rejected_images)} prompt={len(rejected_prompts)}",
        })

        if action == "approve":
            shared.pop("_i2v_regen_image_slots", None)
            shared.pop("_i2v_regen_prompt_slots", None)

        shared["_current_step"] = "validate_i2v_done"
        await _set_state(**_shared_snapshot(shared))
        return "default" if action == "approve" else action