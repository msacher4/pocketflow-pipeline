import asyncio
import copy
import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode, AsyncFlow

from config import TG_VALIDATION_TIMEOUT
from helpers.state import _set_state, _get_state, _shared_snapshot, _save_sub_shared
from helpers.call_agent import reset_agents
from helpers.send_telegram import send_and_wait_validation
log = logging.getLogger("pocketflow-pipeline")


class ResetNode(AsyncNode):
    async def prep_async(self, shared):
        return shared

    async def exec_async(self, shared):
        log.info("Reset: restarting agents...")
        await reset_agents()
        shared["pipeline_id"] = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        shared["started_at"] = datetime.now(timezone.utc).isoformat()
        shared["steps"] = []
        shared["_current_step"] = "reset_done"
        return "ok"

    async def post_async(self, shared, prep, exec):
        shared["steps"].append({"step": "reset", "status": "ok", "ts": datetime.now(timezone.utc).isoformat()})
        await _set_state(**_shared_snapshot(shared))
        return "default"


class ValidationTGNode(AsyncNode):
    def __init__(self, step_name: str, format_msg: callable, buttons_config: list[list[tuple[str, str]]], timeout: int = TG_VALIDATION_TIMEOUT):
        super().__init__(max_retries=1, wait=5)
        self.step_name = step_name
        self.format_msg = format_msg
        self.buttons_config = buttons_config
        self.timeout = timeout

    async def prep_async(self, shared):
        shared["_current_step"] = self.step_name
        await _set_state(**_shared_snapshot(shared))
        return {
            "pipeline_id": shared.get("pipeline_id", "unknown"),
            "shared": shared,
        }

    async def exec_async(self, data):
        vid = f"{data['pipeline_id']}_{self.step_name}"
        text, extra_vid = self.format_msg(data["shared"])
        if extra_vid:
            vid = extra_vid
        buttons = [[
            {"text": label, "callback_data": f"{action}:{vid}"}
            for label, action in row
        ] for row in self.buttons_config]
        return await send_and_wait_validation(vid, text, buttons, self.timeout)

    async def post_async(self, shared, prep, exec):
        shared["_current_step"] = f"{self.step_name}_done"
        shared["steps"].append({
            "step": self.step_name, "status": exec,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        await _set_state(**_shared_snapshot(shared))
        return exec


class ValidateVFNode(ValidationTGNode):
    def __init__(self):
        def _fmt(shared):
            sv = shared.get("selected_video", {})
            vid = f"{shared.get('pipeline_id', '?')}_pf"
            text = (
                f"Results to validate\n\n"
                f"Topic: {shared.get('topic', '?')}\n"
                f"URL: {sv.get('url', '?')}\n"
                f"Description: {sv.get('description', '?')}"
            )
            return text, vid
        buttons = [
            [("Approve -> ScriptWriter", "approve")],
            [("Re-analyse TikTok", "reject")],
        ]
        super().__init__("validate_vf", _fmt, buttons)


class SubFlowNode(AsyncNode):
    def __init__(self, step_name: str, flow_builder: callable, inputs: list[str], outputs: list[str]):
        super().__init__(max_retries=1, wait=5)
        self.step_name = step_name
        self.flow_builder = flow_builder
        self.inputs = inputs
        self.outputs = outputs

    async def prep_async(self, shared):
        sub = {}
        for k in self.inputs:
            if k in shared:
                sub[k] = copy.deepcopy(shared[k])
        for ctx_key in ("pipeline_id", "started_at", "topic", "_traces"):
            if ctx_key in shared:
                sub[ctx_key] = shared[ctx_key]
        sub["steps"] = list(shared.get("steps", []))
        sub["_current_step"] = self.step_name
        await _set_state(**_shared_snapshot(shared))
        return sub

    async def _run_async(self, shared):
        p = await self.prep_async(shared)
        try:
            e = await self._exec(p)
        except Exception as exc:
            _save_sub_shared(self.step_name, p)
            shared["_current_step"] = f"{self.step_name}_error"
            shared["_error"] = f"SubFlow {self.step_name} failed: {type(exc).__name__}: {exc}"
            shared["steps"].append({
                "step": self.step_name, "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            raise
        return await self.post_async(shared, p, e)

    async def exec_async(self, sub):
        flow = self.flow_builder()
        action = await flow.run_async(sub)
        return action, sub

    async def post_async(self, shared, prep, exec):
        action, sub = exec
        _save_sub_shared(self.step_name, sub)

        sub_steps = sub.get("steps", [])
        shared.setdefault("steps", []).extend(sub_steps)

        sub_error = sub.get("_error")
        if sub_error:
            shared["_error"] = sub_error
            shared["_current_step"] = f"{self.step_name}_error"
            await _set_state(**_shared_snapshot(shared))
            return "error"

        shared.setdefault("steps", []).append({
            "step": self.step_name, "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        for k in self.outputs:
            if k in sub:
                shared[k] = copy.deepcopy(sub[k])

        if "_traces" in sub:
            parent_traces = shared.setdefault("_traces", {})
            parent_traces.update(sub["_traces"])

        shared["_current_step"] = f"{self.step_name}_done"
        await _set_state(**_shared_snapshot(shared))
        return "default"


class ValidationSubFlowNode(SubFlowNode):
    """SubFlowNode that propagates the internal flow's action to the parent."""

    async def post_async(self, shared, prep, exec):
        action, sub = exec
        _save_sub_shared(self.step_name, sub)

        sub_steps = sub.get("steps", [])
        shared.setdefault("steps", []).extend(sub_steps)

        sub_error = sub.get("_error")
        if sub_error:
            shared["_error"] = sub_error
            shared["_current_step"] = f"{self.step_name}_error"
            await _set_state(**_shared_snapshot(shared))
            return "error"

        shared.setdefault("steps", []).append({
            "step": self.step_name, "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        for k in self.outputs:
            if k in sub:
                shared[k] = copy.deepcopy(sub[k])

        if "_traces" in sub:
            parent_traces = shared.setdefault("_traces", {})
            parent_traces.update(sub["_traces"])

        shared["_current_step"] = f"{self.step_name}_done"
        await _set_state(**_shared_snapshot(shared))
        return action


class RouteChoiceNode(AsyncNode):
    """Nœud Telegram qui propose de choisir entre le chemin normal et alternatif."""

    def __init__(self, timeout: int = TG_VALIDATION_TIMEOUT):
        super().__init__(max_retries=1, wait=5)
        self.timeout = timeout

    async def prep_async(self, shared):
        shared["_current_step"] = "route_choice"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        from config import TG_BOT_TOKEN
        from helpers.send_telegram import send_telegram
        from helpers.state import _register_validation, _pending_validations, _attach_message, is_auto_approve

        vid = f"{shared.get('pipeline_id', 'unknown')}_pf_route"
        text = (
            f"Choix du chemin pipeline\n\n"
            f"Topic: {shared.get('topic', '?')}\n\n"
            f"Quel chemin veux-tu prendre ?"
        )

        if is_auto_approve():
            # Mode test : chemin alt forcé, pas de question Telegram.
            log.info("[tg] auto-approve route -> alt")
            return shared.get("route", "alt")

        if not TG_BOT_TOKEN:
            log.info("[tg] SKIP route choice (no token), defaulting to normal")
            return "normal"

        buttons = [[
            {"text": "Chemin normal (VF+VA)", "callback_data": f"route_normal:{vid}"},
            {"text": "Actufinder", "callback_data": f"route_alt:{vid}"},
        ]]

        event = _register_validation(vid)
        msg_id = await send_telegram(text, buttons)
        if msg_id:
            _attach_message(vid, msg_id)

        try:
            await asyncio.wait_for(event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            _pending_validations.pop(vid, None)
            return "normal"

        entry = _pending_validations.pop(vid, {})
        result = entry.get("result", "normal")
        if result == "route_normal":
            return "normal"
        elif result == "route_alt":
            return "alt"
        return "normal"

    async def post_async(self, shared, prep, exec):
        shared["_route_choice"] = exec
        shared["_current_step"] = "route_choice_done"
        shared["steps"].append({
            "step": "route_choice", "status": exec,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        await _set_state(**_shared_snapshot(shared))
        return exec
