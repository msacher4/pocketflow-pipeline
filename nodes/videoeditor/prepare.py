import asyncio
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from helpers.ffmpeg import ffprobe
from helpers.state import _set_state, _shared_snapshot, _set_traces

log = logging.getLogger("pocketflow-pipeline")


class VideoEditorPrepareNode(AsyncNode):
    def __init__(self, output_root: str = "output"):
        super().__init__(max_retries=1, wait=5)
        self.output_root = Path(output_root)

    async def prep_async(self, shared):
        shared["_current_step"] = "videoeditor_prepare"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        structure = shared.get("montage_structure", {})
        segments = structure.get("segments", [])
        audio_tracks = structure.get("audio_tracks", [])

        pid = shared.get("pipeline_id", "ve")
        project_dir = self.output_root / pid
        project_dir.mkdir(parents=True, exist_ok=True)

        copied_videos: dict[str, str] = {}
        copied_audio: dict[str, str] = {}
        for seg in segments:
            src = Path(seg["file"])
            if not src.exists():
                raise RuntimeError(f"VideoEditorPrepare: video missing: {src}")
            info = await ffprobe(str(src))
            dest = project_dir / f"src_{seg['index']}{src.suffix or '.webm'}"
            await asyncio.to_thread(shutil.copy2, str(src), str(dest))
            copied_videos[str(seg["index"])] = {
                "src": str(src),
                "dest": str(dest),
                "duration_s": info["duration_s"],
                "width": info["width"],
                "height": info["height"],
            }
            log.info(f"VideoEditorPrepare: copied {src.name} -> {dest.name} ({info['duration_s']:.2f}s)")

        for tr in audio_tracks:
            src = Path(tr["path"])
            if not src.exists():
                log.warning(f"VideoEditorPrepare: audio missing, skipping: {src}")
                continue
            info = await ffprobe(str(src))
            dest = project_dir / f"audio_{tr['ref']}{src.suffix or '.mp3'}"
            await asyncio.to_thread(shutil.copy2, str(src), str(dest))
            copied_audio[tr["ref"]] = {
                "src": str(src),
                "dest": str(dest),
                "duration_s": info["duration_s"],
            }
            log.info(f"VideoEditorPrepare: copied {src.name} -> {dest.name} ({info['duration_s']:.2f}s)")

        return {
            "project_dir": str(project_dir),
            "videos": copied_videos,
            "audio": copied_audio,
        }

    async def post_async(self, shared, prep, exec):
        shared["ve_project_dir"] = exec["project_dir"]
        shared["ve_copied_videos"] = exec["videos"]
        shared["ve_copied_audio"] = exec["audio"]
        shared["_current_step"] = "videoeditor_prepare_done"
        shared["steps"].append({
            "step": "videoeditor_prepare", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": f"{len(exec['videos'])} videos, {len(exec['audio'])} audio",
            "output": exec["project_dir"],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
