import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, LLMJSONQuoteError

log = logging.getLogger("pocketflow-pipeline")


class MontageCriticNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=4, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "montage_critic"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("montage_critic")
        plan = shared.get("montage_plan", "")
        videos = shared.get("generated_videos", [])
        ctx = (
            f"Valide le plan de montage.\n"
            f"Script:\n{shared.get('script', '')[:1000]}\n\n"
            f"Clips générés (4s chacun):\n{json.dumps(videos, ensure_ascii=False)[:3000]}\n\n"
            f"Plan de montage:\n{plan[:4000]}\n\n"
            f"Retourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"N'utilise JAMAIS de guillemets doubles (\" ) dans le contenu : "
            f"remplace-les par des apostrophes simples (').\n"
            f"Format : {{\"approve\": true/false, \"issues\": [...], \"details\": \"...\"}}"
        )
        retry_hint = shared.get("_llm_retry_hint", "")
        if retry_hint:
            ctx += f"\n--- REPONSE PRECEDENTE INVALIDE ---\n{retry_hint}\n"
        resp = await call_llm(LLM_MODEL, soul, ctx)
        _trace_llm(shared, "montage_critic", "exec", LLM_MODEL, soul, ctx, resp)
        retry_hint = shared.get("_llm_retry_hint", "")
        try:
            decision = _extract_json(resp)
        except LLMJSONQuoteError as e:
            shared["_llm_retry_hint"] = (
                "Ta réponse contenait des guillemets doubles non échappés "
                "dans une valeur string. Remplace TOUS les guillemets doubles "
                "par des apostrophes simples (')."
            )
            raise
        log.info(f"MontageCritic -> approve={decision.get('approve')}")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("MontageCritic POST -> exec is not valid JSON")
            shared["_current_step"] = "montage_critic_error"
            shared["_error"] = "MontageCritic: exec is not valid JSON"
            shared["steps"].append({
                "step": "montage_critic", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("montage_plan", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("MontageCritic aborted: exec is not valid JSON")

        approved = decision.get("approve", False)
        if approved:
            shared["assets"] = shared.get("montage_plan", "")
            shared["_current_step"] = "montage_critic_done"
            shared["steps"].append({
                "step": "montage_critic", "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("montage_plan", "")[:500],
                "output": "approved",
            })
            await _set_state(**_shared_snapshot(shared))
            await _set_traces(shared.get("_traces", {}))
            return "default"

        issues = decision.get("issues", [])
        details = decision.get("details", "Plan incomplet")
        shared["_reformat_error"] = f"Issues: {', '.join(issues[:3])}. {details}"
        shared["_reformat_attempts"] = shared.get("_reformat_attempts", 0) + 1
        attempts = shared["_reformat_attempts"]

        if attempts > 3:
            shared["_current_step"] = "montage_critic_error"
            shared["_error"] = f"MontageCritic: max reformat attempts exceeded: {details}"
            shared["steps"].append({
                "step": "montage_critic", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("montage_plan", "")[:500],
                "output": f"reformatted x{attempts}: {details}",
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            return "error"

        shared["_current_step"] = "montage_critic_reformat"
        shared["steps"].append({
            "step": "montage_critic", "status": "reformat",
            "ts": datetime.now(timezone.utc).isoformat(),
            "issues": issues,
            "attempt": attempts,
        })
        await _set_state(**_shared_snapshot(shared))
        return "reformat"
