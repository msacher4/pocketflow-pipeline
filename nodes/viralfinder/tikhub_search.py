import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces, _save_sub_shared
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, _trace_agent
from helpers.call_tikhub_api import call_tikhub_api
from mcp.tikhub_mcp_server import filter_json

log = logging.getLogger("pocketflow-pipeline")


class TikHubSearchNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "tikhub_search"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("tikhub_search")
        ctx = (
            f"Phase: EXEC — génère les paramètres de recherche TikHub\n"
            f"Thème: {shared.get('topic', '')}\n"
            f"Pipeline ID: {shared.get('pipeline_id', 'unknown')}\n\n"
            f"Retourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Format attendu : {{\"keyword\": \"...\", \"count\": 10, \"sort_type\": 1, \"publish_time\": 7}}"
        )
        directive = shared.get("video_feedback", "")
        if directive:
            ctx += (
                f"\n\n--- USER DIRECTIVE (à respecter IMPÉRATIVEMENT) ---\n"
                f"{directive}\n"
                f"RÈGLE DURE : si la directive mentionne une langue, le keyword DOIT être "
                f"rédigé dans cette langue. Ex: directive 'vidéo en français', thème "
                f"'motivation fitness' → keyword 'motivation sport'. "
                f"Un keyword dans une langue différente de la directive = ÉCHEC."
            )
        llm_resp = await call_llm(LLM_MODEL, soul, ctx)
        _trace_llm(shared, "tikhub_search", "exec", LLM_MODEL, soul, ctx, llm_resp)
        params = _extract_json(llm_resp)
        log.info(f"TS EXEC -> params={params}")

        keyword = params.get("keyword", shared.get("topic", ""))
        count = params.get("count", 10)
        sort_type = params.get("sort_type", 1)
        publish_time = params.get("publish_time", 7)

        raw = await call_tikhub_api(
            "/api/v1/tiktok/app/v3/fetch_general_search_result",
            {"keyword": keyword, "count": count, "sort_type": sort_type, "publish_time": publish_time},
        )
        filtered = filter_json(raw, max_items=5)
        _trace_agent(shared, "tikhub_search", "tikhub_api",
                     response=json.dumps(raw, ensure_ascii=False)[:2000])
        log.info(f"TS EXEC -> filtered from {len(json.dumps(raw))} to {len(json.dumps(filtered))} chars")
        return json.dumps(filtered, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        shared["raw_tiktok_results"] = exec
        shared["_current_step"] = "tikhub_search_done"
        shared["steps"].append({
            "step": "tikhub_search", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("topic", ""),
            "output": str(exec)[:10000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        _save_sub_shared("viralfinder", shared)
        return "default"
