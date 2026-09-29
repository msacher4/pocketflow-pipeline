import asyncio
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces

log = logging.getLogger("pocketflow-pipeline")

COMFYUI_BASE = "http://127.0.0.1:8188"


class ComfyUIStart(AsyncNode):
    """Relance le service ComfyUI après un stop (pour sd-cli)."""

    def __init__(self, step: str = "comfyui_start"):
        super().__init__(max_retries=3, wait=10)
        self.step = step

    async def prep_async(self, shared):
        shared["_current_step"] = self.step
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        import aiohttp

        log.info("Starting ComfyUI service...")
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "start", "comfyui.service",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.warning(f"ComfyUI start failed (rc={proc.returncode}): {stderr.decode()[:200]}")

        for i in range(30):
            await asyncio.sleep(2)
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                    async with session.get(f"{COMFYUI_BASE}/system_stats") as resp:
                        if resp.status == 200:
                            log.info(f"ComfyUI ready after {(i+1)*2}s")
                            return {"status": "started"}
            except Exception:
                continue
        log.warning("ComfyUI not reachable after 60s, continuing anyway")
        return {"status": "started_unreachable"}

    async def post_async(self, shared, prep, exec):
        status = "ok" if isinstance(exec, dict) and exec.get("status") == "started" else "warning"
        shared["_current_step"] = f"{self.step}_done"
        shared["steps"].append({
            "step": self.step, "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": str(exec),
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        if shared.get("_slots_to_regenerate"):
            return "regen_done"
        return "default"
