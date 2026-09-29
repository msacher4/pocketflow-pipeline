import asyncio
import logging

log = logging.getLogger("pocketflow-pipeline")


async def ytdlp_metadata(url: str) -> dict:
    cmd = [
        "yt-dlp", "--skip-download",
        "--print", "title",
        "--print", "description",
        "--print", "duration",
        "--print", "view_count",
        "--print", "id",
        "--print", "ext",
        url
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning(f"yt-dlp metadata failed for {url}: {stderr.decode()[:200]}")
        return {"url": url, "error": stderr.decode()[:500]}
    lines = stdout.decode().strip().split("\n")
    if len(lines) < 6:
        return {"url": url, "error": "insufficient output"}
    dur_str = lines[2]
    try:
        duration = float(dur_str) if dur_str.replace(".", "").isdigit() else 0
    except ValueError:
        duration = 0
    vc_str = lines[3]
    try:
        view_count = int(vc_str) if vc_str.isdigit() else 0
    except ValueError:
        view_count = 0
    return {
        "url": url,
        "title": lines[0],
        "description": lines[1][:2000],
        "duration": duration,
        "view_count": view_count,
        "id": lines[4],
        "ext": lines[5],
    }


async def ytdlp_download_clip(url: str, output: str, start: str, end: str) -> bool:
    cmd = [
        "yt-dlp", "-f", "best",
        "-o", output,
        "--download-sections", f"*{start}-{end}",
        "--force-keyframes-at-cuts",
        url
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning(f"yt-dlp clip download failed: {stderr.decode()[:300]}")
        return False
    return True


async def ytdlp_download_audio(url: str, output: str) -> bool:
    cmd = [
        "yt-dlp", "-f", "bestaudio",
        "-x", "--audio-format", "mp3",
        "-o", output,
        url
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning(f"yt-dlp audio download failed: {stderr.decode()[:300]}")
        return False
    return True


async def ytdlp_download_full(url: str, output: str) -> bool:
    cmd = ["yt-dlp", "-f", "best", "-o", output, url]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning(f"yt-dlp full download failed: {stderr.decode()[:300]}")
        return False
    return True
