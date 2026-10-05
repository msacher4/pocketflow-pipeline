import asyncio
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.jev_omni import stop_server as stop_jev_server

log = logging.getLogger("pocketflow-pipeline")

LLAMA_PROXY_BASE = "http://localhost:8080/api/proxy"


class CleanupLlamaJev(AsyncNode):
    """Stop llama-proxy + llama-server Jev, pour libérer la VRAM avant ComfyUI.

    Nœud dédié au flow ALT, entre ValidateCharacterRefs et le Klein ref gen.
    `CleanupLlamaProxy` ne cleans que le proxy 8080 : il ignore Jev, qui reste
    chargé (~7 Go) pendant que Klein monte à ~15,9 Go sur 16 Go, et la
    cohabitation échoue. D'où un nœud distinct plutôt qu'un CleanupLlamaProxy
    réutilisé — le graphe doit montrer que les DEUX serveurs tombent.

    Squelette (prep/exec/post, trace `steps`, retour "default") aligné sur
    CleanupLlamaProxy. Volontairement SANS drop_caches : CombinedCleanup en fait
    un peu, et ça demande sudo — inutile ici, on libère de la VRAM.
    """

    def __init__(self):
        super().__init__(max_retries=2, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "cleanup_llama_jev"
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
                    results["llama_stop"] = await resp.json()
                    log.info(f"Stop llama-proxy: {results['llama_stop']}")

                await asyncio.sleep(3)

                async with session.post(f"{LLAMA_PROXY_BASE}/cleanup") as resp:
                    results["llama_cleanup"] = await resp.json()
                    log.info(f"Cleanup llama-proxy: {results['llama_cleanup']}")
        except Exception as e:
            log.warning(f"Cleanup llama-proxy failed: {type(e).__name__}: {e}")
            results["llama_error"] = str(e)

        # 2. Jev : c'est le noeud qu'il y a sur la carte. Ne lève pas — un serveur
        # déjà arrêté est normal (run précédent, daemon redémarré).
        try:
            results["jev"] = await asyncio.to_thread(stop_jev_server)
            log.info(f"Cleanup Jev-Omni: {results['jev'].get('status')}")
        except Exception as e:
            log.warning(f"Cleanup Jev failed: {type(e).__name__}: {e}")
            results["jev_error"] = str(e)

        # 3. Laisse le pilote ROCm rendre la VRAM avant que Klein ne monte.
        await asyncio.sleep(3)

        return results

    async def post_async(self, shared, prep, exec):
        status = "ok"
        parts = []
        if isinstance(exec, dict):
            for k, v in exec.items():
                if k.endswith("error"):
                    status = "error"
                if isinstance(v, dict):
                    parts.append(f"{k}={v.get('status', '?')}")
                else:
                    parts.append(f"{k}={v}")
            # Serveur Jev encore joignable après l'arrêt = VRAM non rendue,
            # Klein va probablement échouer. On le remonte comme erreur.
            jev = exec.get("jev") or {}
            if jev.get("status") in ("failed", "still_up"):
                status = "error"
        output = ", ".join(parts) if parts else "unexpected result"

        shared["_current_step"] = "cleanup_llama_jev_done"
        shared["steps"].append({
            "step": "cleanup_llama_jev", "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": output,
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
