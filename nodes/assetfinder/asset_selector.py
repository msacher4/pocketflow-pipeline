import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm

log = logging.getLogger("pocketflow-pipeline")


class AssetSelectorNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "asset_selector"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("asset_selector")
        blueprint = shared.get("asset_blueprint", {})
        analyses = shared.get("asset_analyses", {}).get("analyses", [])
        ctx = (
            f"Sélectionne les assets (frames 5-10s) pour le montage.\n"
            f"Thème: {shared.get('topic', '')}\n"
            f"Script:\n{shared.get('script', '')[:2000]}\n\n"
            f"Blueprint slots visuels:\n{json.dumps(blueprint.get('slots', []), ensure_ascii=False)[:3000]}\n\n"
            f"Blueprint audio:\n{json.dumps(blueprint.get('audio', []), ensure_ascii=False)[:1500]}\n\n"
            f"Analyses des assets téléchargés:\n{json.dumps(analyses, ensure_ascii=False)[:8000]}\n\n"
            f"Retourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Format : {{\"selected_assets\": [...]}}"
        )
        resp = await call_llm(LLM_MODEL, soul, ctx)
        _trace_llm(shared, "asset_selector", "exec", LLM_MODEL, soul, ctx, resp)
        decision = _extract_json(resp)
        n = len(decision.get("selected_assets", []))
        log.info(f"AssetSelector -> {n} selected assets")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("AssetSelector POST -> exec is not valid JSON")
            shared["_current_step"] = "asset_selector_error"
            shared["_error"] = "AssetSelector: exec is not valid JSON"
            shared["steps"].append({
                "step": "asset_selector", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("AssetSelector aborted: exec is not valid JSON")

        selected = decision.get("selected_assets", [])
        if not selected:
            log.warning("AssetSelector POST -> selected_assets is empty")
            shared["_current_step"] = "asset_selector_error"
            shared["_error"] = "AssetSelector: empty selected_assets"
            shared["steps"].append({
                "step": "asset_selector", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("AssetSelector aborted: empty selected_assets")

        shared["selected_assets"] = selected
        shared["_current_step"] = "asset_selector_done"
        shared["steps"].append({
            "step": "asset_selector", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
