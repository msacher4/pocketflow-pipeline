import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import TG_BOT_TOKEN, TG_CHAT_ID, TG_VALIDATION_TIMEOUT
from helpers.state import _set_state, _shared_snapshot, _register_validation, _pending_validations
from helpers.send_telegram import send_telegram, send_video_tg, send_audio_tg

log = logging.getLogger("pocketflow-pipeline")


SLOT_LABELS = {
    1: "Clip vidéo 1", 2: "Clip vidéo 2", 3: "Clip vidéo 3",
    "a1": "Musique 1", "a2": "Musique 2",
    "v1": "Voix off 1", "v2": "Voix off 2",
}

SLOT_TYPE_MAP = {
    1: "video", 2: "video", 3: "video",
    "a1": "music", "a2": "music",
    "v1": "voiceover", "v2": "voiceover",
}


async def _send_one_asset(a: dict, vid: str) -> tuple[str, int | None]:
    """Envoie un asset sur Telegram ; retourne (slot_id, msg_id|None)."""
    sid = a.get("slot_id", "?")
    asset_type = a.get("type", "unknown")
    path = a.get("path", a.get("video_path", a.get("image_path", "")))

    if not path or not os.path.isfile(path):
        log.warning(f"ValidateAssets: {asset_type} [{sid}] missing: {path}")
        return sid, None

    caption = _format_caption(a)
    buttons = [
        [
            {"text": "✅ Accepter", "callback_data": f"approve:{vid}:{sid}"},
            {"text": "🔄 Regénérer", "callback_data": f"regen:{vid}:{sid}"},
        ],
        [
            {"text": "🎨 Regen I2V", "callback_data": f"i2v:{vid}:{sid}"},
        ],
    ]

    if asset_type == "video":
        msg_id = await send_video_tg(path, caption, buttons)
    elif asset_type in ("music", "voiceover"):
        msg_id = await send_audio_tg(path, caption, buttons)
    elif asset_type == "image":
        msg_id = await _send_photo_tg(path, caption, buttons)
    else:
        msg_id = await send_telegram(caption, buttons)
    return sid, msg_id


async def _send_assets_for_validation(assets: list[dict], vid: str) -> dict[str, int]:
    """Send each asset to Telegram with per-slot approve/reject buttons."""
    if not TG_BOT_TOKEN:
        log.info("[tg] SKIP (no token): asset validation")
        return {}

    sem = asyncio.Semaphore(3)
    fail_labels = {}

    async def _limited(a):
        async with sem:
            return await _send_one_asset(a, vid)

    results = await asyncio.gather(*(_limited(a) for a in assets))

    msg_ids = {}
    for sid, msg_id in results:
        if msg_id:
            msg_ids[sid] = msg_id
        else:
            label = SLOT_LABELS.get(sid, str(sid))
            fail_labels[sid] = label

    if fail_labels:
        await send_telegram(
            "⚠️ Certains assets n'ont pas pu être envoyés sur Telegram et sont "
            "exclus de la validation :\n" + "\n".join(f"- {l} [{s}]" for s, l in fail_labels.items())
        )
        log.warning(f"ValidateAssets: assets non envoyés: {list(fail_labels)}")

    return msg_ids


async def _send_photo_tg(path: str, caption: str, buttons: list | None = None) -> int | None:
    """Send a photo to Telegram via sendPhoto (compression auto si > 10 Mo)."""
    from helpers.state import is_auto_approve
    if is_auto_approve():
        log.info("[tg] auto-approve skip _send_photo_tg")
        return None
    if not TG_BOT_TOKEN:
        return None
    import httpx, os
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
    except Exception as e:
        log.warning(f"send_photo_tg error: {e}")
    finally:
        if path != original and os.path.isfile(path):
            os.unlink(path)
    return None


def _format_caption(asset: dict) -> str:
    """Format a caption for an asset based on its type."""
    sid = asset.get("slot_id", "?")
    asset_type = asset.get("type", "unknown")
    section = asset.get("section", "")

    if asset_type == "video":
        content = asset.get("content", asset.get("expected", ""))[:100]
        dur = asset.get("duration_s", "?")
        return f"[{sid}] {section} — vidéo\n{content}\ndurée: {dur}s"

    elif asset_type == "voiceover":
        text = asset.get("text", "")[:120]
        return f"[{sid}] {section} — voix off\n{text}"

    elif asset_type == "music":
        mood = asset.get("mood", "")
        return f"[{sid}] {section} — musique\n{mood}"

    return f"[{sid}] {asset_type} — {section}"


def _find_asset_by_slot(assets: list[dict], slot_id) -> dict | None:
    """Find an asset by its slot_id."""
    for a in assets:
        if a.get("slot_id") == slot_id:
            return a
    return None


class ValidateImages(AsyncNode):
    """Validations séquentielles : tous les assets d'abord, puis un slot rejeté à la fois."""

    def __init__(self, step_name: str = "validate_af", timeout: int = TG_VALIDATION_TIMEOUT):
        super().__init__(max_retries=1, wait=5)
        self.step_name = step_name
        self.timeout = timeout

    async def prep_async(self, shared):
        shared["_current_step"] = self.step_name
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        # Mode i2v : 2 phases de validation distinctes (image puis vidéo)
        i2v_phase = shared.get("_i2v_phase")
        if i2v_phase == "image":
            return await self._validate_i2v_image(shared)
        if i2v_phase == "video":
            return await self._validate_i2v_video(shared)

        videos = shared.get("generated_videos", [])
        audio = shared.get("downloaded_audio", [])

        all_assets = []
        for v in videos:
            all_assets.append({**v, "type": "video"})
        for a in audio:
            all_assets.append(a)

        if not all_assets:
            log.info("ValidateImages: no assets to validate")
            return json.dumps({"action": "approve"})

        reject_queue = shared.get("_pending_reject_queue", [])

        if not reject_queue:
            return await self._validate_all(shared, all_assets)
        else:
            return await self._validate_single(shared, all_assets, reject_queue)

    async def _validate_all(self, shared, all_assets):
        """Première validation : envoie tous les assets."""
        base_vid = f"{shared.get('pipeline_id', 'unknown')}_pf_af"

        msg_ids = await _send_assets_for_validation(all_assets, base_vid)
        if not msg_ids:
            log.info("ValidateImages: no assets sent to TG (auto-approve)")
            return json.dumps({"action": "approve"})

        events = []
        for sid in msg_ids:
            slot_vid = f"{base_vid}_{sid}"
            event = _register_validation(slot_vid)
            events.append((sid, slot_vid, event))

        try:
            await asyncio.wait_for(
                asyncio.gather(*(e.wait() for _, _, e in events)),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            for _, sv, _ in events:
                _pending_validations.pop(sv, None)
            shared["_video_action"] = "regen"
            log.info(f"ValidateImages timeout, auto-rejecting all")
            return json.dumps({"action": "reject", "rejected_slots": list(msg_ids.keys()), "feedback": ""})

        approved_slots = []
        rejected_slots = []
        action_by_slot = {}
        for sid, sv, _ in events:
            entry = _pending_validations.pop(sv, {})
            result = entry.get("result", "reject")
            if result.startswith("approve"):
                approved_slots.append(sid)
            elif result.startswith("regen:"):
                rejected_slots.append(sid)
                action_by_slot[sid] = "regen"
            elif result.startswith("i2v:"):
                rejected_slots.append(sid)
                action_by_slot[sid] = "i2v"
            else:
                rejected_slots.append(sid)
                action_by_slot[sid] = "regen"

        log.info(f"ValidateImages initial: approved={approved_slots} rejected={rejected_slots}")

        if not rejected_slots:
            return json.dumps({"action": "approve"})

        first_action = action_by_slot.get(rejected_slots[0], "regen")
        shared["_video_action"] = first_action

        queue = list(rejected_slots)
        shared["_pending_reject_queue"] = queue
        shared["_slots_to_regenerate"] = []
        shared["_current_reject_slot"] = queue[0]
        shared["_current_reject_label"] = SLOT_LABELS.get(queue[0], str(queue[0]))
        shared["_current_reject_type"] = SLOT_TYPE_MAP.get(queue[0], "unknown")

        return json.dumps({
            "action": "reject",
            "rejected_slot": queue[0],
            "rejected_slots": queue,
            "remaining_queue": queue[1:],
            "feedback": "",
        })

    async def _validate_single(self, shared, all_assets, reject_queue):
        """Re-validation : envoie uniquement le slot en cours de traitement."""
        current_slot = reject_queue[0]
        asset = _find_asset_by_slot(all_assets, current_slot)

        if not asset:
            log.warning(f"ValidateImages: slot {current_slot} not found in assets, skipping")
            shared["_pending_reject_queue"] = reject_queue[1:]
            if reject_queue[1:]:
                return json.dumps({"action": "reject", "rejected_slot": reject_queue[1], "rejected_slots": reject_queue[1:], "remaining_queue": reject_queue[1:], "feedback": ""})
            return json.dumps({"action": "approve"})

        base_vid = f"{shared.get('pipeline_id', 'unknown')}_pf_af"
        msg_ids = await _send_assets_for_validation([asset], base_vid)

        if not msg_ids:
            log.info(f"ValidateImages: slot {current_slot} not sent (auto-approve)")
            shared["_pending_reject_queue"] = reject_queue[1:]
            if reject_queue[1:]:
                next_slot = reject_queue[1]
                shared["_current_reject_slot"] = next_slot
                shared["_current_reject_label"] = SLOT_LABELS.get(next_slot, str(next_slot))
                shared["_current_reject_type"] = SLOT_TYPE_MAP.get(next_slot, "unknown")
                return json.dumps({"action": "reject", "rejected_slot": next_slot, "rejected_slots": reject_queue[1:], "remaining_queue": reject_queue[1:], "feedback": ""})
            return json.dumps({"action": "approve"})

        events = []
        for sid in msg_ids:
            slot_vid = f"{base_vid}_{sid}"
            event = _register_validation(slot_vid)
            events.append((sid, slot_vid, event))

        try:
            await asyncio.wait_for(
                asyncio.gather(*(e.wait() for _, _, e in events)),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            for _, sv, _ in events:
                _pending_validations.pop(sv, None)
            log.info(f"ValidateImages timeout for slot {current_slot}, auto-reject")
            return json.dumps({"action": "reject", "rejected_slot": current_slot, "rejected_slots": [current_slot], "remaining_queue": reject_queue[1:], "feedback": ""})

        result = "reject"
        for sid, sv, _ in events:
            entry = _pending_validations.pop(sv, {})
            result = entry.get("result", "reject")

        remaining = reject_queue[1:]

        if result.startswith("approve"):
            log.info(f"ValidateImages: slot {current_slot} approved, {len(remaining)} remaining")
            shared["_pending_reject_queue"] = remaining
            if remaining:
                next_slot = remaining[0]
                shared["_current_reject_slot"] = next_slot
                shared["_current_reject_label"] = SLOT_LABELS.get(next_slot, str(next_slot))
                shared["_current_reject_type"] = SLOT_TYPE_MAP.get(next_slot, "unknown")
                next_action = shared.get("_video_action", "regen")
                shared["_video_action"] = next_action
                return json.dumps({"action": "reject", "rejected_slot": next_slot, "rejected_slots": remaining, "remaining_queue": remaining, "feedback": ""})
            return json.dumps({"action": "approve"})
        else:
            action = "regen"
            if result.startswith("i2v:"):
                action = "i2v"
            elif result.startswith("regen:"):
                action = "regen"
            shared["_video_action"] = action
            log.info(f"ValidateImages: slot {current_slot} action={action}, retry feedback")
            return json.dumps({"action": "reject", "rejected_slot": current_slot, "rejected_slots": [current_slot], "remaining_queue": remaining, "feedback": ""})

    async def _validate_i2v_image(self, shared):
        """Phase 1 i2v : valide l'image régénérée du slot avant de lancer la vidéo i2v."""
        slots = shared.get("_slots_to_regenerate", [])
        slot = slots[0] if slots else shared.get("_rejected_slot")
        images = shared.get("generated_images", [])
        img = _find_asset_by_slot(images, slot)

        if not img:
            log.warning(f"ValidateImages i2v image: slot {slot} no image found")
            shared["_i2v_phase"] = "video"
            return json.dumps({"action": "i2v_video", "rejected_slot": slot})

        base_vid = f"{shared.get('pipeline_id', 'unknown')}_pf_i2v_img"
        asset = {**img, "type": "image", "path": img.get("image_path")}
        caption = _format_caption(asset)
        buttons = [
            [
                {"text": "✅ Image OK", "callback_data": f"approve:{base_vid}:{slot}"},
                {"text": "🔄 Refaire l'image", "callback_data": f"i2v:{base_vid}:{slot}"},
            ],
        ]
        msg_id = await _send_photo_tg(asset["path"], caption, buttons)
        if not msg_id:
            log.info(f"ValidateImages i2v image: no send (auto-approve) slot {slot}")
            shared["_i2v_phase"] = "video"
            return json.dumps({"action": "i2v_video", "rejected_slot": slot})

        slot_vid = f"{base_vid}_{slot}"
        event = _register_validation(slot_vid)
        try:
            await asyncio.wait_for(event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            _pending_validations.pop(slot_vid, None)
            log.info(f"ValidateImages i2v image timeout slot {slot}, regenerate image")
            return json.dumps({"action": "i2v", "rejected_slot": slot, "feedback": ""})

        entry = _pending_validations.pop(slot_vid, {})
        result = entry.get("result", "reject")

        for i, g in enumerate(shared["generated_images"]):
            if g.get("slot_id") == slot:
                shared["generated_images"][i]["confirmed"] = result.startswith("approve")
                break

        if result.startswith("approve"):
            log.info(f"ValidateImages i2v image approved slot {slot} -> gen video")
            shared["_i2v_phase"] = "video"
            return json.dumps({"action": "i2v_video", "rejected_slot": slot})
        else:
            log.info(f"ValidateImages i2v image rejected slot {slot} -> redo image")
            return json.dumps({"action": "i2v", "rejected_slot": slot, "feedback": ""})

    async def _validate_i2v_video(self, shared):
        """Phase 2 i2v : valide la vidéo générée depuis l'image approuvée."""
        slot = shared.get("_rejected_slot")
        videos = shared.get("generated_videos", [])
        vid = _find_asset_by_slot(videos, slot)

        if not vid:
            log.warning(f"ValidateImages i2v video: slot {slot} no video found")
            shared["_i2v_phase"] = ""
            shared["_video_action"] = "regen"
            shared.pop("_slots_to_regenerate", None)
            return json.dumps({"action": "approve", "rejected_slot": slot})

        base_vid = f"{shared.get('pipeline_id', 'unknown')}_pf_i2v_vid"
        asset = {**vid, "type": "video"}
        caption = _format_caption(asset)
        buttons = [
            [
                {"text": "✅ Accepter", "callback_data": f"approve:{base_vid}:{slot}"},
                {"text": "🔄 Regen I2V", "callback_data": f"i2v:{base_vid}:{slot}"},
            ],
        ]
        msg_id = await send_video_tg(asset.get("video_path", vid.get("path")), caption, buttons)
        if not msg_id:
            log.info(f"ValidateImages i2v video: no send (auto-approve) slot {slot}")
            shared["_i2v_phase"] = ""
            shared["_video_action"] = "regen"
            shared.pop("_slots_to_regenerate", None)
            return json.dumps({"action": "approve", "rejected_slot": slot})

        slot_vid = f"{base_vid}_{slot}"
        event = _register_validation(slot_vid)
        try:
            await asyncio.wait_for(event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            _pending_validations.pop(slot_vid, None)
            log.info(f"ValidateImages i2v video timeout slot {slot}, regenerate")
            shared["_i2v_phase"] = "image"
            shared["_slots_to_regenerate"] = [slot]
            return json.dumps({"action": "i2v", "rejected_slot": slot, "feedback": ""})

        entry = _pending_validations.pop(slot_vid, {})
        result = entry.get("result", "reject")

        if result.startswith("approve"):
            log.info(f"ValidateImages i2v video approved slot {slot}")
            shared["_i2v_phase"] = ""
            shared["_video_action"] = "regen"
            shared.pop("_slots_to_regenerate", None)
            return json.dumps({"action": "approve", "rejected_slot": slot})
        else:
            log.info(f"ValidateImages i2v video rejected slot {slot} -> redo image+video")
            shared["_i2v_phase"] = "image"
            shared["_slots_to_regenerate"] = [slot]
            return json.dumps({"action": "i2v", "rejected_slot": slot, "feedback": ""})

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            data = {"action": "reject"}

        action = data.get("action", "reject")
        rejected_slot = data.get("rejected_slot")
        rejected_slots = data.get("rejected_slots", [])

        if rejected_slot:
            shared["_rejected_slot"] = rejected_slot
            shared["_rejected_slot_label"] = SLOT_LABELS.get(rejected_slot, str(rejected_slot))
            shared["_rejected_slot_type"] = SLOT_TYPE_MAP.get(rejected_slot, "unknown")

        if rejected_slots:
            shared["_rejected_slots"] = rejected_slots
            shared["_rejected_slot_labels"] = [SLOT_LABELS.get(s, str(s)) for s in rejected_slots]

        if action == "approve":
            shared.pop("_rejected_slot", None)
            shared.pop("_rejected_slot_label", None)
            shared.pop("_rejected_slot_type", None)
            shared.pop("_slots_to_regenerate", None)

        shared.setdefault("steps", []).append({
            "step": self.step_name, "status": action,
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"slot={rejected_slot} remaining={len(data.get('remaining_queue', []))}" if action == "reject" else "",
        })
        shared["_current_step"] = f"{self.step_name}_done"
        await _set_state(**_shared_snapshot(shared))
        if shared.get("_i2v_phase"):
            return action
        shared.pop("_slots_to_regenerate", None)
        return action
