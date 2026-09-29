"""ScriptReviewer — relit le script en tant que Kal (bandeur de waifu) et
réécrit uniquement les VO qu'il aurait swipées ou sur lesquelles il a hésité."""

import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm
from .worker_common import build_common_context

log = logging.getLogger("pocketflow-pipeline")


class ScriptReviewerNode(AsyncNode):
    """Relecture émotionnelle : le soul (Kal) lit le script VO par VO, sur un
    écran de scrolling, et ne réécrit que les VO qui le font swiper/hésiter."""

    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "script_reviewer"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("script_reviewer")
        script = shared.get("script", "")
        ctx = build_common_context(shared)
        ctx += (
            f"\n--- SCRIPT À REVOIR ---\n{script}\n--- FIN SCRIPT ---\n\n"
            f"Relis le script ci-dessus, VO après VO, comme décrit dans ta "
            f"situation. Pour chaque VO, laisse ton verdict instinctif décider : "
            f"pouce continue, pouce hésite, ou swipe. Réécris SEULEMENT les VO "
            f"que tu hésites à garder ou que tu swipes, avec ta mécanique "
            f"générique. Ne touche à aucune ligne `Video:`. Ne change ni la "
            f"structure ni les plans que tu gardes. Retourne UNIQUEMENT un JSON "
            f"valide : {{\"script\": \"le script complet relu et corrigé\"}}"
        )
        llm_resp = await call_llm(LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, max_tokens=8192, timeout=600)
        _trace_llm(shared, "script_reviewer", "exec", LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, llm_resp)
        decision = _extract_json(llm_resp)
        log.info(f"ScriptReviewer EXEC -> script length={len(decision.get('script', ''))}")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("ScriptReviewer POST -> exec is not valid JSON")
            shared["_current_step"] = "script_reviewer_error"
            shared["_error"] = "ScriptReviewer: exec is not valid JSON"
            shared["steps"].append({
                "step": "script_reviewer", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ScriptReviewer aborted: exec is not valid JSON")

        raw_script = decision.get("script", "")
        if not raw_script.strip():
            log.warning("ScriptReviewer POST -> script is empty")
            shared["_current_step"] = "script_reviewer_error"
            shared["_error"] = "ScriptReviewer: empty script"
            shared["steps"].append({
                "step": "script_reviewer", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ScriptReviewer aborted: empty script")

        shared["script"] = raw_script
        shared["_current_step"] = "script_reviewer_done"
        shared["steps"].append({
            "step": "script_reviewer", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("script", "")[:500],
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"