import asyncio
import logging

import httpx

from config import AGENT_URLS, AGENT_TOKENS, AGENT_SERVICES

log = logging.getLogger("pocketflow-pipeline")

async def call_agent(agent: str, payload: dict, timeout: int = 600) -> str:
    url = AGENT_URLS[agent]
    token = AGENT_TOKENS[agent]
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    message = payload.get("content", payload.get("message", str(payload)))
    body = {"message": message}
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=body, headers=headers)
        r.raise_for_status()
        text = r.text
        log.info(f"Agent {agent} responded ({len(text)} chars)")
        return text

async def reset_agents():
    async def _restart_one(svc: str):
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", svc,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        log.info(f"Reset: restarted {svc}")

    await asyncio.gather(*[_restart_one(s) for s in AGENT_SERVICES])
