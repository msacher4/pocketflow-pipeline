import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot
from helpers.analyze_video import analyze_video

log = logging.getLogger("pocketflow-pipeline")


class VideoDownloadNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "video_download"
        await _set_state(**_shared_snapshot(shared))
        sv = shared.get("selected_video", {})
        url = sv.get("url", "")
        return url

    async def exec_async(self, url):
        if not url:
            raise RuntimeError("No video URL to download")
        log.info(f"Downloading & analyzing video: {url[:60]}...")
        result = await analyze_video(url)
        log.info(f"Analysis complete ({len(result['analysis'])} chars)")
        return result

    async def post_async(self, shared, prep, exec):
        shared["video_path"] = exec["video_path"]
        shared["duration_s"] = exec["duration_s"]
        shared["resolution"] = exec["resolution"]
        shared["cuts"] = exec["cuts"]
        shared["analysis"] = exec["analysis"]
        shared["video_analysis"] = {
            "duration_s": exec["duration_s"],
            "resolution": exec["resolution"],
            "cuts": exec["cuts"],
            "analysis": exec["analysis"],
        }
        shared["_current_step"] = "video_download_done"
        shared["steps"].append({
            "step": "video_download", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "duration_s": exec["duration_s"],
            "resolution": exec["resolution"],
        })
        await _set_state(**_shared_snapshot(shared))
        return "default"
