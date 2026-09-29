import asyncio
import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.analyze_video import analyze_video_path

log = logging.getLogger("pocketflow-pipeline")

SEMAPHORE_LIMIT = 2


async def _analyze_one(asset: dict, semaphore: asyncio.Semaphore) -> dict:
    path = asset.get("path", "")
    if not path:
        return {**asset, "error": "no path"}
    if asset.get("type") == "audio":
        return {**asset, "analysis": "audio: selection by metadata only"}
    async with semaphore:
        try:
            result = await analyze_video_path(path)
        except Exception as e:
            log.warning(f"asset_analysis failed for {path}: {type(e).__name__}: {e}")
            return {**asset, "error": f"{type(e).__name__}: {e}"}
    return {
        **asset,
        "video_path": result["video_path"],
        "duration_s": result["duration_s"],
        "resolution": result["resolution"],
        "cuts": result["cuts"],
        "analysis": result["analysis"],
    }


class AssetAnalysisNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "asset_analysis"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        assets = shared.get("downloaded_assets", {}).get("assets", [])
        if not assets:
            raise RuntimeError("asset_analysis: no downloaded assets")

        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        analyzed = await asyncio.gather(*[_analyze_one(a, semaphore) for a in assets])

        ok = [a for a in analyzed if not a.get("error")]
        log.info(f"AssetAnalysis -> {len(ok)}/{len(analyzed)} analyzed")
        if not ok:
            raise RuntimeError("asset_analysis: all analyses failed")

        return json.dumps({"analyses": analyzed}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("AssetAnalysis POST -> exec is not valid JSON")
            shared["_current_step"] = "asset_analysis_error"
            shared["_error"] = "AssetAnalysis: exec is not valid JSON"
            shared["steps"].append({
                "step": "asset_analysis", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("AssetAnalysis aborted: exec is not valid JSON")

        shared["asset_analyses"] = data
        shared["_current_step"] = "asset_analysis_done"
        shared["steps"].append({
            "step": "asset_analysis", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
