import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import TG_BOT_TOKEN, TG_VALIDATION_TIMEOUT
from helpers.state import _set_state, _shared_snapshot, _register_validation, _pending_validations, _attach_message
from helpers.send_telegram import send_telegram

log = logging.getLogger("pocketflow-pipeline")


class TGSendValidationNode(AsyncNode):
    """Send validation message with approve/reject buttons, wait for decision."""

    def __init__(self, step_name: str, format_msg: callable, buttons_config: list, timeout: int = TG_VALIDATION_TIMEOUT):
        super().__init__(max_retries=1, wait=5)
        self.step_name = step_name
        self.format_msg = format_msg
        self.buttons_config = buttons_config
        self.timeout = timeout

    async def prep_async(self, shared):
        shared["_current_step"] = self.step_name
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        vid = f"{shared.get('pipeline_id', 'unknown')}_{self.step_name}"
        text, extra_vid = self.format_msg(shared)
        if extra_vid:
            vid = extra_vid

        if not TG_BOT_TOKEN:
            log.info(f"[tg] SKIP (no token): {text[:60]}...")
            return "approve"

        event = _register_validation(vid)
        msg_id = await send_telegram(text, [[
            {"text": label, "callback_data": f"{action}:{vid}"}
            for label, action in row
        ] for row in self.buttons_config])
        if msg_id:
            _attach_message(vid, msg_id)

        try:
            import asyncio
            await asyncio.wait_for(event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            _pending_validations.pop(vid, None)
            return "reject"

        entry = _pending_validations.pop(vid, {})
        return entry.get("result", "reject")

    async def post_async(self, shared, prep, exec):
        shared["_current_step"] = f"{self.step_name}_done"
        shared["steps"].append({
            "step": self.step_name, "status": exec,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        await _set_state(**_shared_snapshot(shared))
        return exec
