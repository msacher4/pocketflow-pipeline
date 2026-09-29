"""Tension Worker — structure la montée de tension émotionnelle du BODY alt."""

import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm
from .worker_common import build_common_context

log = logging.getLogger("pocketflow-pipeline")


class TensionWorkerNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "tension_worker"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("tension_worker")
        ctx = (
            build_common_context(shared)
            + "\n\nRetourne UNIQUEMENT ton tension_plan en JSON valide, sans texte avant ni après."
        )
        llm_resp = await call_llm(LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, max_tokens=16384, timeout=600)
        _trace_llm(shared, "tension_worker", "exec", LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, llm_resp)
        decision = _extract_json(llm_resp)
        log.info(f"TensionWorker EXEC -> tension_plan ok: {len(json.dumps(decision))} chars")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("TensionWorker POST -> exec is not valid JSON")
            shared["_current_step"] = "tension_worker_error"
            shared["_error"] = "TensionWorker: exec is not valid JSON"
            shared["steps"].append({
                "step": "tension_worker", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("topic", ""),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("TensionWorker aborted: exec is not valid JSON")

        shared["tension_plan"] = decision.get("tension_plan", decision)
        shared["_current_step"] = "tension_worker_done"
        shared["steps"].append({
            "step": "tension_worker", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("topic", ""),
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        log.info("TensionWorker POST -> tension_plan saved to shared['tension_plan']")
        return "default"