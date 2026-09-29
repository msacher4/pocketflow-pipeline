import os
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot

log = logging.getLogger("pocketflow-pipeline")


class VideoCleanupNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "video_cleanup"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        video_path = shared.get("video_path", "")
        if video_path and os.path.isfile(video_path):
            os.remove(video_path)
            log.info(f"Deleted video: {video_path}")

        removed = 0
        for key in ("video_path",):
            if key in shared:
                del shared[key]
                removed += 1

        return {"deleted_video": bool(video_path), "cleaned_keys": removed}

    async def post_async(self, shared, prep, exec):
        shared["_current_step"] = "video_cleanup_done"
        shared["steps"].append({
            "step": "video_cleanup", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "deleted_video": exec["deleted_video"],
        })
        await _set_state(**_shared_snapshot(shared))
        return "default"
