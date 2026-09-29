import logging
import os
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.ffmpeg import ffprobe
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.send_telegram import send_telegram

log = logging.getLogger("pocketflow-pipeline")


class VideoEditorFinalizeNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "videoeditor_finalize"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        final_path = shared.get("ve_final_path")
        if not final_path or not os.path.exists(final_path):
            raise RuntimeError(f"VideoEditorFinalize: final.mp4 missing: {final_path}")

        info = await ffprobe(final_path)
        project_dir = shared.get("ve_project_dir", "")
        title = shared.get("montage_structure", {}).get("title", shared.get("topic", "montage"))
        clips = shared.get("ve_clips", [])
        n_audio = shared.get("ve_copied_audio", {})
        structure = shared.get("montage_structure", {})
        n_audio_tracks = len(structure.get("audio_tracks", []))

        log_lines = [
            "--- VideoEditor montage report ---",
            f"project_dir: {project_dir}",
            f"final: {final_path}",
            f"duration_s: {info['duration_s']:.2f}",
            f"resolution: {info['width']}x{info['height']}",
            f"vcodec: {info['vcodec']}",
            f"acodec: {info['acodec']}",
            "clips:",
        ]
        for c in clips:
            log_lines.append(f"  [{c['index']}] {c['section']}: {c['path']} ({c['start_s']:.2f}-{c['end_s']:.2f}s)")
        log_lines.append("audio:")
        for ref, a in n_audio.items():
            log_lines.append(f"  [{ref}]: {a['dest']}")
        log_path = os.path.join(project_dir, "log.txt")
        with open(log_path, "w") as f:
            f.write("\n".join(log_lines) + "\n")

        report = (
            f"## Résultat Montage — {title}\n\n"
            f"✅ Montage terminé\n"
            f"📁 Fichier : {final_path}\n"
            f"⏱️ Durée : {info['duration_s']:.1f}s\n"
            f"📐 Résolution : {info['width']}x{info['height']}\n"
            f"🎬 Segments : {len(clips)} clips assemblés\n"
            f"🔊 Pistes audio : {n_audio_tracks}\n\n"
            f"### Opérations\n"
            f"- clip : {len(clips)} segments découpés → ok\n"
            f"- concat : assemblage des clips → ok\n"
            f"- audio : mix de {n_audio_tracks} pistes → ok\n"
            f"- finalize : vérification ffprobe → ok\n"
        )
        log.info(f"VideoEditorFinalize: {final_path} ({info['duration_s']:.2f}s, {info['width']}x{info['height']})")
        return {"report": report, "info": info, "log_path": log_path}

    async def post_async(self, shared, prep, exec):
        shared["video_result"] = exec["report"]
        shared["ve_final_info"] = exec["info"]
        shared["finished_at"] = datetime.now(timezone.utc).isoformat()
        shared["_current_step"] = "pipeline_complete"
        shared["pipeline_result"] = "success"
        shared["steps"].append({
            "step": "videoeditor_finalize", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("ve_final_path", ""),
            "output": exec["report"][:500],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
