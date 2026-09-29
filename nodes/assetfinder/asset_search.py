import asyncio
import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces

log = logging.getLogger("pocketflow-pipeline")

SEMAPHORE_LIMIT = 3
CANDIDATES_PER_SLOT = 3


def _search_youtube(query: str, limit: int = CANDIDATES_PER_SLOT) -> list[dict]:
    from youtubesearchpython import VideosSearch
    vs = VideosSearch(query, limit=limit)
    r = vs.result()
    results = []
    for item in r.get("result", []):
        if item.get("type") != "video":
            continue
        views = item.get("viewCount", {})
        results.append({
            "url": item.get("link", ""),
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "duration": item.get("duration", ""),
            "viewCount": views.get("text", "") if isinstance(views, dict) else str(views),
            "channel": (item.get("channel") or {}).get("name", "") if isinstance(item.get("channel"), dict) else "",
            "published": item.get("publishedTime", ""),
        })
    return results


async def _search_slot(slot: dict, semaphore: asyncio.Semaphore) -> dict:
    keywords = slot.get("keywords") or [slot.get("content", "")]
    query = " ".join(keywords)
    async with semaphore:
        try:
            candidates = await asyncio.to_thread(_search_youtube, query)
        except Exception as e:
            log.warning(f"asset_search failed for '{query}': {type(e).__name__}: {e}")
            candidates = []
    return {"slot_id": slot.get("id"), "type": slot.get("type", "visual"), "query": query, "candidates": candidates}


class AssetSearchNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "asset_search"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        slots = blueprint.get("slots", []) + blueprint.get("audio", [])
        if not slots:
            raise RuntimeError("asset_search: blueprint empty (no slots)")

        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        results = await asyncio.gather(*[_search_slot(s, semaphore) for s in slots])

        resolved = []
        for r in results:
            if r["candidates"]:
                resolved.append(r)

        log.info(f"AssetSearch -> {len(resolved)}/{len(slots)} slots with candidates")
        if not resolved:
            raise RuntimeError("asset_search: no candidates found for any slot")

        return json.dumps({"searched": resolved}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("AssetSearch POST -> exec is not valid JSON")
            shared["_current_step"] = "asset_search_error"
            shared["_error"] = "AssetSearch: exec is not valid JSON"
            shared["steps"].append({
                "step": "asset_search", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("AssetSearch aborted: exec is not valid JSON")

        shared["asset_search"] = data
        shared["_current_step"] = "asset_search_done"
        shared["steps"].append({
            "step": "asset_search", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": f"blueprint slots={len(shared.get('asset_blueprint', {}).get('slots', []))}, audio={len(shared.get('asset_blueprint', {}).get('audio', []))}",
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
