import asyncio
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.comfyui_api import comfyui_free

log = logging.getLogger("pocketflow-pipeline")

COMFYUI_BASE = "http://127.0.0.1:8188"


class ComfyUIFreeMemory(AsyncNode):
    """Libère la VRAM entre les étapes de génération (Klein → LTX → Upscale).

    stop_service=True : stop le service systemd ComfyUI (libère TOUTE la VRAM).
    stop_service=False : appelle juste /free (défaut, libère les modèles mais pas le service).
    """

    def __init__(self, step: str = "comfyui_free", stop_service: bool = False):
        super().__init__(max_retries=1, wait=5)
        self.step = step
        self.stop_service = stop_service

    async def prep_async(self, shared):
        shared["_current_step"] = self.step
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        if self.stop_service:
            log.info("Stopping ComfyUI service (full VRAM release)...")
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "stop", "comfyui.service",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                log.info("ComfyUI service stopped")
            else:
                log.warning(f"ComfyUI stop failed (rc={proc.returncode}): {stderr.decode()[:200]}")
            await asyncio.sleep(5)
        else:
            log.info("Freeing VRAM (unload_models + free_memory)...")
            await comfyui_free(unload_models=True, free_memory=True)

    async def post_async(self, shared, prep, exec):
        shared["_current_step"] = f"{self.step}_done"
        shared["steps"].append({
            "step": self.step, "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": "ComfyUI stopped" if self.stop_service else "VRAM freed",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
