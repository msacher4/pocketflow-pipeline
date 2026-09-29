"""Visual Worker — conçoit la shot list image par image du script alt."""

import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm
from .worker_common import build_common_context

log = logging.getLogger("pocketflow-pipeline")


class VisualWorkerNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "visual_worker"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("visual_worker")
        ctx = (
            build_common_context(shared)
            + "\n\nRetourne UNIQUEMENT ton visual_plan en JSON valide, sans texte avant ni après."
        )
        llm_resp = await call_llm(LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, max_tokens=16384, timeout=600)
        _trace_llm(shared, "visual_worker", "exec", LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, llm_resp)
        decision = _extract_json(llm_resp)
        log.info(f"VisualWorker EXEC -> visual_plan ok: {len(json.dumps(decision))} chars")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("VisualWorker POST -> exec is not valid JSON")
            shared["_current_step"] = "visual_worker_error"
            shared["_error"] = "VisualWorker: exec is not valid JSON"
            shared["steps"].append({
                "step": "visual_worker", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("topic", ""),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VisualWorker aborted: exec is not valid JSON")

        shared["visual_plan"] = decision.get("visual_plan", decision)
        shared["_current_step"] = "visual_worker_done"
        shared["steps"].append({
            "step": "visual_worker", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("topic", ""),
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        log.info("VisualWorker POST -> visual_plan saved to shared['visual_plan']")
        return "default"