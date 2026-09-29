import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_MODEL
from helpers.state import _set_state, _shared_snapshot, _save_sub_shared
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm

log = logging.getLogger("pocketflow-pipeline")


class TitleCharacterGateNode(AsyncNode):
    """Gate de pré-validation : vérifie qu'un perso féminin NOM est identifiable.

    Positionné AVANT la validation Telegram (validate_af_news) et avant tout
    fetch/synthèse. Évite à l'utilisateur de valider une news qui sera ensuite
    jetée par extract_character (pas de perso -> pas de visuel I2V).

    - "approve" : perso nommé -> route vers validate_af_news
    - "no_good_news" : aucun perso identifiable -> feeds suivants
    """

    def __init__(self):
        super().__init__(max_retries=2, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_title_gate"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("title_gate")
        article = shared.get("selected_article", {})
        ctx = (
            f"Thème de la vidéo: {shared.get('topic', '')}\n"
            f"Titre: {article.get('title', '')}\n"
            f"Source: {article.get('source', '')}\n"
            f"Angle: {article.get('hook_angle', '')}\n"
            f"Score: {article.get('score', 0)}\n"
            f"\nRenseigne le personnage féminin nommé s'il est identifiable, "
            f"sinon has_character=false."
        )
        resp = await call_llm(LLM_MODEL, soul, ctx, max_tokens=1024, timeout=120)
        _trace_llm(shared, "actufinder_title_gate", "exec", LLM_MODEL, soul, ctx, resp)
        try:
            decision = _extract_json(resp)
        except Exception as e:
            log.warning(f"TitleCharacterGate: invalid JSON ({e}), assume no character")
            return json.dumps({"has_character": False, "character_name": "", "franchise": ""})
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            data = {}

        has_character = bool(data.get("has_character"))
        character_name = str(data.get("character_name", "") or "").strip()

        article = shared.get("selected_article", {})
        if has_character and character_name:
            article["character_name"] = character_name
            article["franchise"] = str(data.get("franchise", "") or "").strip()
            shared["selected_article"] = article
            shared["_current_step"] = "actufinder_title_gate_done"
            shared["steps"].append({
                "step": "actufinder_title_gate", "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": f"perso={character_name} ({article.get('franchise', '')})",
            })
            await _set_state(**_shared_snapshot(shared))
            _save_sub_shared("actufinder", shared)
            return "approve"

        # Aucun personnage identifiable : on exclut l'article et on change de feed.
        url = article.get("url", "")
        if url:
            from .used_articles import mark_used
            mark_used(url)
        shared["_title_gate_attempts"] = shared.get("_title_gate_attempts", 0) + 1
        shared["_actufinder_empty_feed"] = False
        log.info(
            f"ActuFinder: title_gate rejette (pas de perso nommé) "
            f"-> {article.get('title', '')[:70]} (tentative gate "
            f"{shared['_title_gate_attempts']})"
        )
        shared["_current_step"] = "actufinder_title_gate_reject"
        shared["steps"].append({
            "step": "actufinder_title_gate", "status": "no_good_news",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": "pas de personnage féminin nommé",
        })
        await _set_state(**_shared_snapshot(shared))
        _save_sub_shared("actufinder", shared)
        return "no_good_news"