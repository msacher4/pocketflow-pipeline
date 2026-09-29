import asyncio
import json
import logging
import os
import tempfile

import httpx

from config import TG_BOT_TOKEN, TG_CHAT_ID
from .state import _register_validation, _pending_validations

log = logging.getLogger("pocketflow-pipeline")

_MAX_CHARS = 4000
_TG_MAX_VIDEO_SIZE = 50 * 1024 * 1024  # 50 MB

async def send_telegram(text: str, buttons: list | None = None) -> int | None:
    """Send a Telegram message, return sent message_id or None."""
    if not TG_BOT_TOKEN:
        log.info(f"[tg] SKIP (no token): {text[:60]}...")
        return None
    msg_id = None
    async with httpx.AsyncClient(timeout=10) as c:
        for i, chunk in enumerate(_split_text(text)):
            payload = {"chat_id": TG_CHAT_ID, "text": chunk}
            if buttons and i == 0:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            r = await c.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", json=payload)
            if i == 0 and r.status_code == 200:
                result = r.json().get("result", {})
                msg_id = result.get("message_id")
    return msg_id

def _split_text(text: str, max_chars: int = _MAX_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts = []
    while len(text) > max_chars:
        cut = text.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = text.rfind(" ", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        parts.append(text[:cut])
        text = text[cut:]
    parts.append(text)
    return parts

async def send_and_wait_validation(vid: str, text: str, buttons: list, timeout: int) -> str:
    if not TG_BOT_TOKEN:
        log.info(f"[tg] SKIP (no token): {text[:60]}...")
        return "approve"
    event = _register_validation(vid)
    msg_id = await send_telegram(text, buttons)
    if msg_id:
        from .state import _attach_message
        _attach_message(vid, msg_id)
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _pending_validations.pop(vid, None)
        return "reject"
    entry = _pending_validations.pop(vid, {})
    return entry.get("result", "reject")


async def _compress_video(video_path: str) -> str:
    """Compress video to fit under 50 MB. Returns path to compressed file (or original)."""
    size = os.path.getsize(video_path)
    if size <= _TG_MAX_VIDEO_SIZE:
        return video_path
    log.info(f"Video {os.path.basename(video_path)} is {size / 1024 / 1024:.1f} MB, compressing for TG...")
    out = tempfile.mktemp(suffix=".mp4")
    # Target ~40 MB with 2-pass or CRF
    target_bits = 40 * 8 * 1024 * 1024
    # Get duration via ffprobe
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        duration = float(stdout.decode().strip() or "4")
    except Exception:
        duration = 4.0
    bitrate = int(target_bits / duration / 1000)  # kbps
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", video_path,
        "-c:v", "libx264", "-b:v", f"{bitrate}k",
        "-preset", "fast", "-an", out,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    if os.path.isfile(out) and os.path.getsize(out) < size:
        log.info(f"Compressed video: {os.path.getsize(out) / 1024 / 1024:.1f} MB")
        return out
    return video_path


async def _upload_with_retry(
    method: str, path: str, files_field: str, filename: str, mime: str,
    caption: str, buttons: list | None, retries: int = 3, timeout: int = 60,
) -> int | None:
    """Upload `path` via `method` (sendVideo/sendDocument/sendPhoto) with retries
    et logs complets (type + traceback). Retourne le msg_id ou None si échec final."""
    import traceback

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                files = {files_field: (filename, open(path, "rb"), mime)}
                payload = {"chat_id": TG_CHAT_ID, "caption": caption}
                if buttons:
                    payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
                r = await c.post(url, data=payload, files=files)
                if r.status_code == 200:
                    return r.json().get("result", {}).get("message_id")
                log.warning(f"{method} failed ({r.status_code}): {r.text[:200]}")
                if r.status_code in (400, 403):
                    # Erreur durable (fichier invalide, interdit) : inutile de retenter.
                    return None
        except Exception as e:
            log.warning(
                f"{method} upload error (attempt {attempt}/{retries}): "
                f"{type(e).__name__}: {e!r}\n{traceback.format_exc()[:500]}"
            )
        if attempt < retries:
            await asyncio.sleep(2 * attempt)
    return None


async def send_video_tg(video_path: str, caption: str, buttons: list | None = None) -> int | None:
    """Upload a video to Telegram via sendVideo, return msg_id."""
    if not TG_BOT_TOKEN:
        log.info(f"[tg] SKIP send_video (no token): {caption[:60]}...")
        return None
    path = await _compress_video(video_path)
    try:
        return await _upload_with_retry(
            "sendVideo", path, "video", "clip.mp4", "video/mp4",
            caption, buttons, retries=3, timeout=60,
        )
    finally:
        if path != video_path and os.path.isfile(path):
            os.unlink(path)


async def compress_image_for_telegram(image_path: str, max_bytes: int = 10 * 1024 * 1024) -> str:
    """Recompresse une image si elle dépasse `max_bytes` (limite sendPhoto Telegram
    = ~10 Mo). Réduit la dimension max à 2048px puis ajuste la qualité JPEG en
    boucle jusqu'à passer sous le seuil. Renvoie le chemin d'un tmp fichier (à
    supprimer par l'appelant) ou l'original si déjà OK."""
    size = os.path.getsize(image_path)
    if size <= max_bytes:
        return image_path
    log.info(f"Image {os.path.basename(image_path)} {size / 1024 / 1024:.1f} Mo > 10 Mo, compression TG...")
    from PIL import Image
    out = tempfile.mktemp(suffix=".jpg")
    quality = 85
    while quality > 20:
        try:
            with Image.open(image_path) as im:
                im = im.convert("RGB")
                if max(im.size) > 2048:
                    im.thumbnail((2048, 2048), Image.LANCZOS)
                im.save(out, "JPEG", quality=quality, optimize=True)
        except Exception as e:
            log.warning(f"compress_image failed ({e}), fallback original")
            return image_path
        if os.path.getsize(out) <= max_bytes:
            log.info(f"Image compressée: {os.path.getsize(out) / 1024 / 1024:.1f} Mo (q{quality})")
            return out
        quality -= 15
    return image_path


async def send_audio_tg(audio_path: str, caption: str, buttons: list | None = None) -> int | None:
    """Upload an audio file to Telegram via sendDocument, return msg_id."""
    if not TG_BOT_TOKEN:
        log.info(f"[tg] SKIP send_audio (no token): {caption[:60]}...")
        return None
    ext = os.path.splitext(audio_path)[1].lower()
    mime = {"wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
            ".flac": "audio/flac", ".m4a": "audio/mp4"}.get(ext, "audio/wav")
    filename = os.path.basename(audio_path)
    return await _upload_with_retry(
        "sendDocument", audio_path, "document", filename, mime,
        caption, buttons, retries=3, timeout=30,
    )
