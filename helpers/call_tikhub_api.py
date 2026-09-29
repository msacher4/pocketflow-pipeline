import logging
import urllib.parse

import httpx

from config import TIKHUB_API_KEY, TIKHUB_API_BASE

log = logging.getLogger("pocketflow-pipeline")

async def call_tikhub_api(path: str, params: dict) -> dict:
    url = f"{TIKHUB_API_BASE}{path}"
    filtered = {k: v for k, v in params.items() if v is not None}
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in filtered.items())
    full_url = f"{url}?{qs}" if qs else url
    log.info(f"TikHub GET {path} ({filtered})")
    headers = {
        "Authorization": f"Bearer {TIKHUB_API_KEY}",
        "Accept": "application/json",
        "User-Agent": "PocketFlow-Pipeline/1.0",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(full_url, headers=headers)
        r.raise_for_status()
        data = r.json()
        log.info(f"TikHub responded ({len(str(data))} chars)")
        return data
