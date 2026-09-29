import asyncio
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.comfyui_api import comfyui_free

log = logging.getLogger("pocketflow-pipeline")

LLAMA_PROXY_BASE = "http://localhost:8080/api/proxy"


class InitCleanup(AsyncNode):
    """Full reset RAM/VRAM au démarrage du pipeline.

    Kill sd-cli + llama-proxy stop/cleanup + ComfyUI free + drop_caches.
    Identique à CombinedCleanup mais retourne toujours "default" (→ asset_planner).
    """

    def __init__(self, step="init_cleanup"):
        super().__init__(max_retries=2, wait=10)
        self.step = step

    async def prep_async(self, shared):
        shared["_current_step"] = self.step
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        import aiohttp

        results = {}
        timeout = aiohttp.ClientTimeout(total=30)

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                "pkill -f sd-cli 2>/dev/null; pkill -f stable-diffusion.cpp 2>/dev/null; echo ok",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            results["kill_sdcli"] = stdout.decode().strip()
            log.info(f"InitCleanup: kill residual sd-cli: {results['kill_sdcli']}")
        except Exception as e:
            log.warning(f"InitCleanup: kill sd-cli failed: {type(e).__name__}: {e}")
            results["kill_sdcli_error"] = str(e)

        await asyncio.sleep(2)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{LLAMA_PROXY_BASE}/stop") as resp:
                    results["llama_stop"] = await resp.json()
                    log.info(f"InitCleanup: stop llama-proxy: {results['llama_stop']}")
                await asyncio.sleep(3)
                async with session.post(f"{LLAMA_PROXY_BASE}/cleanup") as resp:
                    results["llama_cleanup"] = await resp.json()
                    log.info(f"InitCleanup: cleanup llama-proxy: {results['llama_cleanup']}")
        except Exception as e:
            log.warning(f"InitCleanup: llama cleanup failed: {type(e).__name__}: {e}")
            results["llama_error"] = str(e)

        try:
            await comfyui_free(unload_models=True, free_memory=True)
            results["comfyui_free"] = "ok"
        except Exception as e:
            log.warning(f"InitCleanup: comfyui free failed: {type(e).__name__}: {e}")
            results["comfyui_free_error"] = str(e)

        await asyncio.sleep(3)

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
            log.info(f"InitCleanup: drop_caches: {results['drop_caches']}")
        except Exception as e:
            log.warning(f"InitCleanup: drop_caches failed: {type(e).__name__}: {e}")
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
        return "default"
