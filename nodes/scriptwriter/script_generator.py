import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm

log = logging.getLogger("pocketflow-pipeline")


class ScriptGeneratorNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "script_generator"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("script_generator")
        sv = shared.get("selected_video", {})
        va = shared.get("video_analysis", {})
        reformat_error = shared.get("_reformat_error", "")
        ctx = (
            f"Phase: EXEC — génère le script vidéo\n"
            f"Thème: {shared.get('topic', '')}\n"
            f"URL vidéo source: {sv.get('url', '')}\n"
            f"Description: {sv.get('description', '')[:500]}\n"
        )
        if va:
            ctx += f"Analyse vidéo disponible: {json.dumps(va, ensure_ascii=False)[:3000]}\n"
        ctx += (
            f"\nPipeline ID: {shared.get('pipeline_id', 'unknown')}\n\n"
            f"Retourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Format : {{\"script\": \"script vidéo complet...\"}}\n"
            f"Langues : VO et Video descriptions en anglais."
        )
        if reformat_error:
            ctx += (
                f"\n\n--- REFORMAT REQUIRED ---\n"
                f"Rejeté par validation: {reformat_error}\n"
                f"Corrige le format et retourne EXACTEMENT {{\"script\": \"...\"}}"
            )
            shared["_reformat_error"] = ""
        script_feedback = shared.get("script_feedback", "")
        if script_feedback:
            ctx += (
                f"\n\n--- USER FEEDBACK (à intégrer) ---\n"
                f"{script_feedback}\n"
                f"Intègre ce feedback dans le script réécrit."
            )
            shared["script_feedback"] = ""
        llm_resp = await call_llm(LLM_SCRIPTWRITER_MODEL, soul, ctx, max_tokens=8192, timeout=600)
        _trace_llm(shared, "script_generator", "exec", LLM_SCRIPTWRITER_MODEL, soul, ctx, llm_resp)
        decision = _extract_json(llm_resp)
        log.info(f"SG EXEC -> script length={len(decision.get('script', ''))}")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("SG POST -> exec is not valid JSON")
            shared["_current_step"] = "script_generator_error"
            shared["_error"] = "ScriptGenerator: exec is not valid JSON"
            shared["steps"].append({
                "step": "script_generator", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("topic", ""),
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ScriptGenerator aborted: exec is not valid JSON")

        raw_script = decision.get("script", "")
        if not raw_script.strip():
            log.warning("SG POST -> script is empty")
            shared["_current_step"] = "script_generator_error"
            shared["_error"] = "ScriptGenerator: empty script"
            shared["steps"].append({
                "step": "script_generator", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("topic", ""),
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ScriptGenerator aborted: empty script")

        shared["script"] = raw_script
        shared["_current_step"] = "script_generator_done"
        shared["steps"].append({
            "step": "script_generator", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("topic", ""),
            "output": str(exec)[:10000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
