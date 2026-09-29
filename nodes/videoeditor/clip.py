import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.ffmpeg import clip_segment
from helpers.state import _set_state, _shared_snapshot, _set_traces

log = logging.getLogger("pocketflow-pipeline")


class VideoEditorClipNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "videoeditor_clip"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        structure = shared.get("montage_structure", {})
        segments = sorted(structure.get("segments", []), key=lambda s: s["index"])
        project_dir = shared.get("ve_project_dir")
        if not project_dir:
            raise RuntimeError("VideoEditorClip: no project dir (prepare missing)")

        clips: list[dict] = []
        for seg in segments:
            idx = seg["index"]
            copied = shared.get("ve_copied_videos", {}).get(str(idx), {})
            src = copied.get("dest") or seg["file"]
            duration_s = copied.get("duration_s", 0)
            start = max(0.0, float(seg["start_s"]))
            end = float(seg["end_s"])
            if duration_s and end > duration_s:
                end = duration_s
            if start >= end:
                start = 0.0
                end = duration_s or 1.0
            out = f"{project_dir}/clip_{idx}.mp4"
            await clip_segment(src, out, start, end)
            clips.append({
                "index": idx,
                "section": seg.get("section", ""),
                "path": out,
                "start_s": start,
                "end_s": end,
            })
            log.info(f"VideoEditorClip: seg {idx} ({seg.get('section')}) {start:.2f}-{end:.2f}s -> {out}")

        return {"clips": clips}

    async def post_async(self, shared, prep, exec):
        shared["ve_clips"] = exec["clips"]
        shared["_current_step"] = "videoeditor_clip_done"
        shared["steps"].append({
            "step": "videoeditor_clip", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": f"{len(exec['clips'])} segments",
            "output": f"{len(exec['clips'])} clips découpés",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
