import asyncio
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.comfyui_api import comfyui_free

log = logging.getLogger("pocketflow-pipeline")

LLAMA_PROXY_BASE = "http://localhost:8080/api/proxy"


class CombinedCleanup(AsyncNode):
    """Cleanup complet: kill sd-cli + llama-proxy + ComfyUI VRAM + drop_caches.

    Combine CleanupLlamaProxy + ComfyUIFreeMemory + free_audio
    en un seul appel pour libérer toute la RAM/VRAM avant régénération.
    """

    def __init__(self, step: str = "combined_cleanup", route_to_generator: bool = True):
        super().__init__(max_retries=2, wait=10)
        self.step = step
        self.route_to_generator = route_to_generator

    async def prep_async(self, shared):
        shared["_current_step"] = self.step
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        import aiohttp

        results = {}
        timeout = aiohttp.ClientTimeout(total=30)

        # 0. Kill any residual sd-cli processes
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                "pkill -f sd-cli 2>/dev/null; pkill -f stable-diffusion.cpp 2>/dev/null; echo ok",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            results["kill_sdcli"] = stdout.decode().strip()
            log.info(f"Kill residual sd-cli: {results['kill_sdcli']}")
        except Exception as e:
            log.warning(f"Kill sd-cli failed: {type(e).__name__}: {e}")
            results["kill_sdcli_error"] = str(e)

        # Wait for processes to fully terminate
        await asyncio.sleep(2)

        # 1. Cleanup llama-proxy (stop + cleanup)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{LLAMA_PROXY_BASE}/stop") as resp:
                    results["llama_stop"] = await resp.json()
                    log.info(f"Stop llama-proxy: {results['llama_stop']}")
                await asyncio.sleep(3)
                async with session.post(f"{LLAMA_PROXY_BASE}/cleanup") as resp:
                    results["llama_cleanup"] = await resp.json()
                    log.info(f"Cleanup llama-proxy: {results['llama_cleanup']}")
        except Exception as e:
            log.warning(f"Llama cleanup failed: {type(e).__name__}: {e}")
            results["llama_error"] = str(e)

        # 2. ComfyUI free (unload_models + free_memory)
        #    If regenerating video, stop the service entirely for full VRAM release
        slot_type = shared.get("_rejected_slot_type")
        if slot_type == "video":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "systemctl", "--user", "stop", "comfyui.service",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                results["comfyui_stop"] = "ok" if proc.returncode == 0 else f"rc={proc.returncode}"
                log.info(f"Stop ComfyUI service for video regen: {results['comfyui_stop']}")
            except Exception as e:
                log.warning(f"Stop ComfyUI failed: {type(e).__name__}: {e}")
                results["comfyui_stop_error"] = str(e)
        else:
            try:
                await comfyui_free(unload_models=True, free_memory=True)
                results["comfyui_free"] = "ok"
            except Exception as e:
                log.warning(f"ComfyUI free failed: {type(e).__name__}: {e}")
                results["comfyui_free_error"] = str(e)

        # Wait for VRAM to actually be freed by the driver
        await asyncio.sleep(3)

        # 3. drop_caches (sync RAM) — try without sudo first
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                "sync && tee /proc/sys/vm/drop_caches > /dev/null 2>&1 <<< 3 || "
                "sudo -n bash -c 'sync; echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null || echo nopasswd_required",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode().strip()
            if "nopasswd" in output:
                results["drop_caches"] = "skipped (sudo required)"
            else:
                results["drop_caches"] = "ok"
            log.info(f"drop_caches: {results['drop_caches']}")
        except Exception as e:
            log.warning(f"drop_caches failed: {type(e).__name__}: {e}")
            results["drop_caches_error"] = str(e)

        return results

    async def post_async(self, shared, prep, exec):
        status = "ok"
        parts = []
        if isinstance(exec, dict):
            for k, v in exec.items():
                if "error" in k:
                    status = "error"
                elif isinstance(v, dict):
                    parts.append(f"{k}={v.get('status', '?')}")
                else:
                    parts.append(f"{k}={v}")
        output = ", ".join(parts) if parts else "unexpected"

        shared["_current_step"] = f"{self.step}_done"
        shared["steps"].append({
            "step": self.step, "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": output,
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))

        if not self.route_to_generator:
            return "default"

        slot_type = shared.get("_rejected_slot_type")
        if not slot_type:
            return "default"

        video_action = shared.get("_video_action", "regen")
        if video_action == "i2v":
            if self.step == "i2v_video_cleanup":
                return "i2v_video"
            return "i2v"

        type_to_target = {
            "video": "sdcpp_video_gen",
            "music": "music_generator",
            "voiceover": "voice_generator",
        }
        return type_to_target.get(slot_type, "sdcpp_video_gen")
