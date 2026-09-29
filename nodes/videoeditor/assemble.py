import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.ffmpeg import concat_videos, mix_audio
from helpers.state import _set_state, _shared_snapshot, _set_traces

log = logging.getLogger("pocketflow-pipeline")


class VideoEditorAssembleNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "videoeditor_assemble"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        clips = sorted(shared.get("ve_clips", []), key=lambda c: c["index"])
        if not clips:
            raise RuntimeError("VideoEditorAssemble: no clips (clip step missing)")
        project_dir = shared.get("ve_project_dir")
        if not project_dir:
            raise RuntimeError("VideoEditorAssemble: no project dir")

        clip_paths = [c["path"] for c in clips]
        video_duration = sum(c["end_s"] - c["start_s"] for c in clips)

        concat_out = f"{project_dir}/concat.mp4"
        await concat_videos(clip_paths, concat_out)

        structure = shared.get("montage_structure", {})
        audio_tracks = []
        for tr in structure.get("audio_tracks", []):
            copied = shared.get("ve_copied_audio", {}).get(tr["ref"], {})
            path = copied.get("dest") or tr.get("path")
            if not path:
                continue
            trim_dur = float(tr.get("trim_dur_s") or video_duration)
            audio_tracks.append({
                "path": path,
                "start_s": float(tr.get("start_s", 0)),
                "trim_start_s": float(tr.get("trim_start_s", 0)),
                "trim_dur_s": trim_dur,
                "volume": float(tr.get("volume", 0.9)),
            })

        final_out = f"{project_dir}/final.mp4"
        await mix_audio(concat_out, audio_tracks, final_out, video_duration)

        log.info(f"VideoEditorAssemble: final.mp4 ({video_duration:.2f}s video, {len(audio_tracks)} audio tracks)")

        return {
            "final_path": final_out,
            "concat_path": concat_out,
            "video_duration_s": video_duration,
            "n_clips": len(clips),
            "n_audio": len(audio_tracks),
        }

    async def post_async(self, shared, prep, exec):
        shared["ve_final_path"] = exec["final_path"]
        shared["ve_video_duration_s"] = exec["video_duration_s"]
        shared["_current_step"] = "videoeditor_assemble_done"
        shared["steps"].append({
            "step": "videoeditor_assemble", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": f"{exec['n_clips']} clips, {exec['n_audio']} audio",
            "output": exec["final_path"],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
