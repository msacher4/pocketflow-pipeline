import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from config import SDCPP_UPSCALE_WIDTH, SDCPP_UPSCALE_HEIGHT, SDCPP_SKIP_UPSCALE
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.ffmpeg import upscale_lanczos
from nodes.assetfinder.remap_montage import _remap_montage_paths

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"


def _passthrough(video_path: str, dest_dir: Path, slot_id: str) -> str:
    """En 720p natif : rename clip_<slot_id>.webm -> clip_<slot_id>.webm sans upscale."""
    from pathlib import Path as P
    src = P(video_path)
    dest = dest_dir / f"clip_{slot_id}.webm"
    if src != dest:
        import shutil
        shutil.move(str(src), str(dest))
    return str(dest)


class FfmpegUpscaler(AsyncNode):
    """Upscale les clips en 1080×1920 via ffmpeg lanczos après interpolation RIFE.

    En mode 720p natif (SDCPP_SKIP_UPSCALE=1) : simple passthrough."""

    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "ffmpeg_upscale"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        videos = shared.get("generated_videos", [])
        if not videos:
            raise RuntimeError("ffmpeg_upscale: no generated videos")

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        upscaled = []
        for vid in videos:
            slot_id = vid.get("slot_id", "unknown")
            video_path = vid.get("video_path", "")

            if not video_path:
                log.warning(f"Slot {slot_id}: no video_path, skipping upscale")
                continue

            if SDCPP_SKIP_UPSCALE:
                path = _passthrough(video_path, dest, slot_id)
            else:
                src_path = Path(video_path)
                if src_path.exists() and src_path.parent == dest and src_path.stem == f"clip_{slot_id}":
                    tmp_dir = dest / f"_tmp_upscale_{slot_id}"
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    path = await upscale_lanczos(
                        video_path=video_path,
                        dest_dir=str(tmp_dir),
                        name=f"clip_{slot_id}",
                        width=SDCPP_UPSCALE_WIDTH,
                        height=SDCPP_UPSCALE_HEIGHT,
                    )
                    final = dest / f"clip_{slot_id}.mp4"
                    import shutil
                    shutil.move(path, str(final))
                    shutil.rmtree(str(tmp_dir), ignore_errors=True)
                    path = str(final)
                else:
                    log.info(f"Lanczos upscale to {SDCPP_UPSCALE_WIDTH}x{SDCPP_UPSCALE_HEIGHT} for slot {slot_id}")
                    path = await upscale_lanczos(
                        video_path=video_path,
                        dest_dir=str(dest),
                        name=f"clip_{slot_id}",
                        width=SDCPP_UPSCALE_WIDTH,
                        height=SDCPP_UPSCALE_HEIGHT,
                    )
            upscaled.append({
                "slot_id": slot_id,
                "section": vid.get("section", ""),
                "position": vid.get("position", 0),
                "content": vid.get("content", ""),
                "expected": vid.get("expected", ""),
                "video_path": path or video_path,
                "image_path": vid.get("image_path", ""),
                "duration_s": vid.get("duration_s", 97.0 / 25.0),
            })

        if not upscaled:
            raise RuntimeError("ffmpeg_upscale: no videos processed")

        mode = "720p natif (skip)" if SDCPP_SKIP_UPSCALE else f"lanczos {SDCPP_UPSCALE_WIDTH}x{SDCPP_UPSCALE_HEIGHT}"
        log.info(f"FfmpegUpscaler -> {len(upscaled)}/{len(videos)} videos ({mode})")
        return json.dumps({"upscaled_videos": upscaled}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("FfmpegUpscaler POST -> exec is not valid JSON")
            shared["_current_step"] = "ffmpeg_upscale_error"
            shared["_error"] = "FfmpegUpscaler: exec is not valid JSON"
            shared["steps"].append({
                "step": "ffmpeg_upscale", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("FfmpegUpscaler aborted: exec is not valid JSON")

        shared["generated_videos"] = data.get("upscaled_videos", [])
        _remap_montage_paths(shared, data.get("upscaled_videos", []))
        shared["_current_step"] = "ffmpeg_upscale_done"
        shared["steps"].append({
            "step": "ffmpeg_upscale", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(shared['generated_videos'])} videos upscaled",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
