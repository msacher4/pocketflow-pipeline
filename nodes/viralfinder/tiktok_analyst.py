import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_ANALYST_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces, _save_sub_shared
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, _trace_agent

log = logging.getLogger("pocketflow-pipeline")


class TikTokAnalystNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "tiktok_analyst"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("tiktok_analyst")
        raw = shared.get("raw_tiktok_results", "")

        video_feedback = shared.get("video_feedback", "")
        feedback_block = ""
        if video_feedback:
            feedback_block = (
                f"\n\n--- USER FEEDBACK (à intégrer) ---\n"
                f"{video_feedback}\n"
                f"La conformité à ce feedback PRIME sur l'engagement. "
                f"Si AUCUNE vidéo des résultats ne respecte ce feedback, "
                f"retourne {{\"selected_url\": \"\", \"reason\": \"...\"}}"
            )
            shared["video_feedback"] = ""

        ctx = (
            f"Phase: EXEC — analyse les résultats TikTok\n"
            f"Thème: {shared.get('topic', '')}\n"
            f"{feedback_block}\n\n"
            f"Résultats bruts TikHub:\n{raw[:5000]}\n\n"
            f"Pipeline ID: {shared.get('pipeline_id', 'unknown')}\n\n"
            f"Analyse chaque vidéo : views, likes, comments, shares, calcule l'engagement_rate.\n"
            f"Retourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Format : {{\"selected_url\": \"...\", \"selected_desc\": \"...\"}}"
        )
        reformat_error = shared.get("_reformat_error", "")
        if reformat_error:
            ctx += (
                f"\n\n--- REFORMAT REQUIRED ---\n"
                f"Previous format rejected: {reformat_error}\n"
                f"Fix the JSON format, return EXACTLY "
                f"{{\"selected_url\": \"...\", \"selected_desc\": \"...\"}}"
            )
            shared["_reformat_error"] = ""

        llm_resp = await call_llm(LLM_ANALYST_MODEL, soul, ctx, max_tokens=4096, timeout=600)
        _trace_llm(shared, "tiktok_analyst", "exec", LLM_ANALYST_MODEL, soul, ctx, llm_resp)
        decision = _extract_json(llm_resp)
        log.info(f"TA EXEC -> url={decision.get('selected_url', '')[:60]}")
        _trace_agent(shared, "tiktok_analyst", "analysis",
                     response=json.dumps(decision, ensure_ascii=False)[:2000])
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("TA POST -> exec is not valid JSON")
            shared["_current_step"] = "tiktok_analyst_error"
            shared["_error"] = "TikTokAnalyst: exec is not valid JSON"
            shared["steps"].append({
                "step": "tiktok_analyst", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("raw_tiktok_results", "")[:2000],
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("TikTokAnalyst aborted: exec is not valid JSON")

        selected_url = decision.get("selected_url") or ""
        selected_desc = decision.get("selected_desc") or ""

        if not selected_url:
            reason = decision.get("reason", "")
            search_attempts = shared.get("_search_attempts", 0)
            if reason and search_attempts < 2:
                shared["_search_attempts"] = search_attempts + 1
                shared["video_feedback"] = reason
                log.info(f"TA POST -> escalation to search (attempt {search_attempts + 1}/2, reason={reason[:120]})")
                shared["steps"].append({
                    "step": "tiktok_analyst", "status": "retry",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "input": shared.get("raw_tiktok_results", "")[:2000],
                    "output": str(exec)[:10000],
                    "reason": reason,
                })
                await _set_state(**_shared_snapshot(shared))
                await _set_traces(shared.get("_traces", {}))
                return "search"
            log.warning(f"TA POST -> no selected_url in decision (reason={reason[:120]})")
            shared["_current_step"] = "tiktok_analyst_error"
            shared["_error"] = f"TikTokAnalyst: no video selected" + (f": {reason}" if reason else "")
            shared["steps"].append({
                "step": "tiktok_analyst", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("raw_tiktok_results", "")[:2000],
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("TikTokAnalyst aborted: no video selected")

        shared["selected_video"] = {"url": selected_url, "description": selected_desc}

        log.info(f"TA POST -> selected {selected_url[:60]}")

        shared["_current_step"] = "tiktok_analyst_done"
        shared["steps"].append({
            "step": "tiktok_analyst", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("raw_tiktok_results", "")[:2000],
            "output": str(exec)[:10000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        _save_sub_shared("viralfinder", shared)
        return "default"
