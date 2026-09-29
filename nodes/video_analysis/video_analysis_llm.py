import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot

log = logging.getLogger("pocketflow-pipeline")


class VideoAnalysisLLMNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "video_analysis_llm"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        analysis = shared.get("analysis", "")
        log.info(f"VAL EXEC -> analysis length={len(analysis)}")
        return analysis

    async def post_async(self, shared, prep, exec):
        shared["_current_step"] = "video_analysis_llm_done"
        shared["steps"].append({
            "step": "video_analysis_llm", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        await _set_state(**_shared_snapshot(shared))
        return "default"
