import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm

log = logging.getLogger("pocketflow-pipeline")


class ScriptRefinerNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "script_refiner"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("script_refiner")
        raw = shared.get("raw_script", "")
        reformat_error = shared.get("_reformat_error", "")

        ctx = (
            f"Phase: EXEC — rafine le script brut\n"
            f"Thème: {shared.get('topic', '')}\n"
            f"Script brut:\n{raw[:5000]}\n\n"
            f"Pipeline ID: {shared.get('pipeline_id', 'unknown')}\n\n"
            f"Retourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Format : {{\"script\": \"script raffiné et propre\"}}"
        )
        if reformat_error:
            ctx += (
                f"\n\n--- REFORMAT REQUIRED ---\n"
                f"Previous format rejected: {reformat_error}\n"
                f"Fix the JSON format, return EXACTLY "
                f"{{\"script\": \"...\"}}"
            )
            shared["_reformat_error"] = ""

        llm_resp = await call_llm(LLM_SCRIPTWRITER_MODEL, soul, ctx, max_tokens=8192, timeout=600)
        _trace_llm(shared, "script_refiner", "exec", LLM_SCRIPTWRITER_MODEL, soul, ctx, llm_resp)
        decision = _extract_json(llm_resp)
        log.info(f"SR EXEC -> script length={len(decision.get('script', ''))}")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("SR POST -> exec is not valid JSON")
            shared["_current_step"] = "script_refiner_error"
            shared["_error"] = "ScriptRefiner: exec is not valid JSON"
            shared["steps"].append({
                "step": "script_refiner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("raw_script", "")[:2000],
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ScriptRefiner aborted: exec is not valid JSON")

        err = decision.get("error", "")
        if err:
            log.warning(f"SR POST -> error: {err}")
            shared["_current_step"] = "script_refiner_error"
            shared["_error"] = f"ScriptRefiner: {err}"
            shared["steps"].append({
                "step": "script_refiner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("raw_script", "")[:2000],
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError(f"ScriptRefiner aborted: {err}")

        script = decision.get("script", "")
        if not script.strip():
            log.warning("SR POST -> script is empty")
            shared["_current_step"] = "script_refiner_error"
            shared["_error"] = "ScriptRefiner: empty script"
            shared["steps"].append({
                "step": "script_refiner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("raw_script", "")[:2000],
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ScriptRefiner aborted: empty script")

        shared["script"] = script
        shared["_current_step"] = "script_refiner_done"
        shared["steps"].append({
            "step": "script_refiner", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("raw_script", "")[:2000],
            "output": str(exec)[:10000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
