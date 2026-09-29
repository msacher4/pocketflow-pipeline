"""Combiner — fusionne les 3 plans workers en une direction créative unique."""

import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm
from .worker_common import build_common_context

log = logging.getLogger("pocketflow-pipeline")


class CombinerNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "combiner"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("combiner")
        ctx = (
            build_common_context(shared)
            + "\n===== PLANS DES WORKERS =====\n"
            f"HOOK PLAN:\n{json.dumps(shared.get('hook_plan', {}), ensure_ascii=False)}\n\n"
            f"TENSION PLAN:\n{json.dumps(shared.get('tension_plan', {}), ensure_ascii=False)}\n\n"
            f"VISUAL PLAN:\n{json.dumps(shared.get('visual_plan', {}), ensure_ascii=False)}\n"
            "===== FIN PLANS DES WORKERS =====\n\n"
            "Retourne UNIQUEMENT ta creative_direction en JSON valide, sans texte avant ni après."
        )
        llm_resp = await call_llm(LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, max_tokens=16384, timeout=600)
        _trace_llm(shared, "combiner", "exec", LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, llm_resp)
        decision = _extract_json(llm_resp)
        log.info(f"Combiner EXEC -> creative_direction ok: {len(json.dumps(decision))} chars")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("Combiner POST -> exec is not valid JSON")
            shared["_current_step"] = "combiner_error"
            shared["_error"] = "Combiner: exec is not valid JSON"
            shared["steps"].append({
                "step": "combiner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("topic", ""),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("Combiner aborted: exec is not valid JSON")

        shared["creative_direction"] = decision.get("creative_direction", decision)
        shared["_current_step"] = "combiner_done"
        shared["steps"].append({
            "step": "combiner", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("topic", ""),
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        log.info("Combiner POST -> creative_direction saved to shared['creative_direction']")
        return "default"