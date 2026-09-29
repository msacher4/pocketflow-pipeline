import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.ytdlp import ytdlp_download_full, ytdlp_download_audio

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"
SEMAPHORE_LIMIT = 2


async def _download_video(url: str, dest: Path) -> str | None:
    out = str(dest / f"clip_{abs(hash(url))}.mp4")
    success = await ytdlp_download_full(url, out)
    if not success or not os.path.isfile(out) or os.path.getsize(out) == 0:
        return None
    return out


async def _download_audio(url: str, dest: Path) -> str | None:
    out = str(dest / f"audio_{abs(hash(url))}.mp3")
    success = await ytdlp_download_audio(url, out)
    if not success or not os.path.isfile(out) or os.path.getsize(out) == 0:
        return None
    return out


async def _download_one(item: dict, dest: Path, is_audio: bool, semaphore: asyncio.Semaphore) -> dict:
    url = item.get("url", "")
    if not url:
        return {"slot_id": item.get("slot_id"), "type": item.get("type", "audio" if is_audio else "visual"), "url": url, "path": None}
    async with semaphore:
        path = await (_download_audio(url, dest) if is_audio else _download_video(url, dest))
    return {
        "slot_id": item.get("slot_id"),
        "type": item.get("type", "audio" if is_audio else "visual"),
        "url": url,
        "title": item.get("title", ""),
        "path": path,
    }


class DownloadAssetsNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "download_assets"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        searched = shared.get("asset_search", {}).get("searched", [])
        if not searched:
            raise RuntimeError("download_assets: no search results")

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        tasks = []
        for slot in searched:
            candidates = slot.get("candidates", [])
            if not candidates:
                continue
            top = candidates[0]
            is_audio = slot.get("type") == "audio"
            tasks.append(_download_one(
                {"slot_id": slot.get("slot_id"), "type": slot.get("type"), "url": top.get("url"), "title": top.get("title")},
                dest, is_audio, semaphore,
            ))

        downloaded = await asyncio.gather(*tasks)

        ok = [d for d in downloaded if d.get("path")]
        log.info(f"DownloadAssets -> {len(ok)}/{len(downloaded)} downloaded")
        if not ok:
            raise RuntimeError("download_assets: all downloads failed")

        return json.dumps({"assets": downloaded}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("DownloadAssets POST -> exec is not valid JSON")
            shared["_current_step"] = "download_assets_error"
            shared["_error"] = "DownloadAssets: exec is not valid JSON"
            shared["steps"].append({
                "step": "download_assets", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("DownloadAssets aborted: exec is not valid JSON")

        shared["downloaded_assets"] = data
        shared["_current_step"] = "download_assets_done"
        shared["steps"].append({
            "step": "download_assets", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
