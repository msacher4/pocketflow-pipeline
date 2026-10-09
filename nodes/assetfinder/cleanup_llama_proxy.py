import asyncio
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces

log = logging.getLogger("pocketflow-pipeline")

LLAMA_PROXY_BASE = "http://localhost:8080/api/proxy"


class CleanupLlamaProxy(AsyncNode):
    """Stop + cleanup de llama-proxy pour libérer VRAM/RAM avant ComfyUI."""

    def __init__(self, step="cleanup_llama_proxy"):
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
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 1. Stop le modèle actuel (SIGTERM propre)
                async with session.post(f"{LLAMA_PROXY_BASE}/stop") as resp:
                    results["stop"] = await resp.json()
                    log.info(f"Stop llama-proxy: {results['stop']}")

                # 2. Attendre que le processus se termine
                await asyncio.sleep(3)

                # 3. Cleanup : kill restants + swapoff/swapon
                async with session.post(f"{LLAMA_PROXY_BASE}/cleanup") as resp:
                    results["cleanup"] = await resp.json()
                    log.info(f"Cleanup llama-proxy: {results['cleanup']}")
        except Exception as e:
            log.warning(f"Cleanup llama-proxy failed: {type(e).__name__}: {e}")
            results["error"] = str(e)

        return results

    async def post_async(self, shared, prep, exec):
        status = "ok"
        if isinstance(exec, dict):
            stop = exec.get("stop", {}).get("status", "?")
            cleanup = exec.get("cleanup", {}).get("status", "?")
            if exec.get("error"):
                status = "error"
            output = f"stop={stop}, cleanup={cleanup}"
        else:
            output = "unexpected result"

        shared["_current_step"] = f"{self.step}_done"
        shared["steps"].append({
            "step": self.step, "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": output,
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
