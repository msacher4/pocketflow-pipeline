import asyncio
import base64
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import httpx

from config import LLM_VISION_MODEL, LLM_URL

log = logging.getLogger("pocketflow-pipeline")

VIDEO_DIR = "/media/marcs/Linux_Apps/Projets_AI/pocketflow_pipeline/downloads"
MAX_ANALYSIS_FRAMES = 10


def _run_cmd(args: list[str], timeout: int = 120) -> str:
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


def _download_video(url: str) -> str:
    os.makedirs(VIDEO_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(VIDEO_DIR, f"video_{ts}.mp4")
    _run_cmd([
        "yt-dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", out, "--restrict-filenames", url
    ], timeout=300)
    files = sorted(Path(VIDEO_DIR).glob("*.mp4"), key=os.path.getmtime, reverse=True)
    return str(files[0]) if files else out


def _get_metadata(path: str) -> dict:
    duration = _run_cmd([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", path
    ])
    width = _run_cmd([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width", "-of", "csv=p=0", path
    ])
    height = _run_cmd([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=height", "-of", "csv=p=0", path
    ])
    return {
        "duration_s": round(float(duration), 1),
        "width": int(width),
        "height": int(height),
    }


def _detect_cuts(path: str) -> list[float]:
    r = subprocess.run([
        "ffmpeg", "-i", path,
        "-filter:v", "select='gt(scene,0.15)',showinfo",
        "-f", "null", "-"
    ], capture_output=True, text=True, timeout=120)
    timestamps = sorted(set(
        float(m) for m in re.findall(r'pts_time:([\d.]+)', r.stderr or "")
    ))
    return timestamps


def _extract_frames(path: str, cut_timestamps: list[float]) -> list[str]:
    points = [0.0] + cut_timestamps
    midpoints = []
    for i in range(len(points) - 1):
        mid = (points[i] + points[i + 1]) / 2
        midpoints.append(mid)
    if len(midpoints) > MAX_ANALYSIS_FRAMES:
        step = len(midpoints) / MAX_ANALYSIS_FRAMES
        midpoints = [midpoints[round(i * step)] for i in range(MAX_ANALYSIS_FRAMES)]
    frames_b64 = []
    for i, ts in enumerate(midpoints):
        r = subprocess.run([
            "ffmpeg", "-y", "-ss", str(ts), "-i", path,
            "-vframes", "1", "-q:v", "5", "-f", "image2pipe", "-"
        ], capture_output=True, timeout=60)
        if r.returncode == 0 and r.stdout:
            frames_b64.append(base64.b64encode(r.stdout).decode())
    return frames_b64


async def _call_vision_api(duration: float, cuts_text: str, frames_b64: list[str]) -> str:
    prompt = (
        f"Ces vidéo dure exactement {duration} secondes. "
        f"Les cuts (changements de plan) sont aux timestamps : {cuts_text}\n\n"
        "Pour chaque image fournie (une par plan), décris plan par plan ce qu'on voit à l'écran, "
        "les transitions, le hook, le body, le CTA, les éléments visuels (B-rolls, textes, zooms, cuts), "
        "et audio (musique, SFX). Sois très précis sur le rythme du montage."
    )
    content = [{"type": "text", "text": prompt}]
    for b64 in frames_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    payload = {
        "model": LLM_VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 8192,
    }
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(LLM_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"].get("content") or ""


async def analyze_video_path(video_path: str) -> dict:
    metadata = _get_metadata(video_path)
    cuts = _detect_cuts(video_path)
    frames_b64 = _extract_frames(video_path, cuts)
    cuts_text = ", ".join(f"{c:.2f}" for c in cuts)
    analysis = await _call_vision_api(metadata["duration_s"], cuts_text, frames_b64)
    return {
        "video_path": video_path,
        "duration_s": metadata["duration_s"],
        "resolution": f"{metadata['width']}x{metadata['height']}",
        "cuts": cuts,
        "analysis": analysis,
    }


async def analyze_video(url: str) -> dict:
    video_path = await asyncio.to_thread(_download_video, url)
    return await analyze_video_path(video_path)
