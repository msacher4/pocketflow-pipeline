import json
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, field_validator
from pocketflow import AsyncNode

from helpers.state import (
    _set_state, _shared_snapshot, _save_sub_shared,
)

log = logging.getLogger("pocketflow-pipeline")


class SelectedArticle(BaseModel):
    title: str
    url: str
    source: str
    published_at: str
    score: int
    character_name: str
    franchise: str
    reason: str
    hook_angle: str

    @field_validator("title", "url", "hook_angle", "character_name", "franchise")
    @classmethod
    def not_empty(cls, v):
        if not v or not str(v).strip():
            raise ValueError("champ vide")
        return v.strip()

    @field_validator("score")
    @classmethod
    def score_min(cls, v):
        if not isinstance(v, int) or v < 75:
            raise ValueError("score insuffisant (< 75)")
        return v


class PydanticAFValidationNode(AsyncNode):
    """Valide l'article sélectionné par le LLM avec un schéma pydantic.

    - "default" quand l'article est valide (passe à la validation Telegram news)
    - "reformat" quand il est invalide (reboucle vers LLMSelectNode)
    - "error" après MAX_REFORMAT tentatives infructueuses
    """

    MAX_REFORMAT = 3

    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "pydantic_af_validation"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        article = shared.get("selected_article", {})
        try:
            SelectedArticle(**article)
            return {"valid": True}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    async def post_async(self, shared, prep, exec):
        if exec["valid"]:
            shared["_current_step"] = "pydantic_af_validation_done"
            shared["steps"].append({
                "step": "pydantic_af_validation", "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": json.dumps(shared.get("selected_article", {}), ensure_ascii=False)[:2000],
            })
            await _set_state(**_shared_snapshot(shared))
            _save_sub_shared("actufinder", shared)
            return "default"

        error = exec["error"]
        shared["_reformat_error"] = f"Article invalide ({error}). Réécris le JSON avec des champs valides."
        shared["_reformat_attempts"] = shared.get("_reformat_attempts", 0) + 1
        attempts = shared["_reformat_attempts"]

        shared["_current_step"] = "pydantic_af_validation_reformat"
        shared["steps"].append({
            "step": "pydantic_af_validation", "status": "reformat",
            "ts": datetime.now(timezone.utc).isoformat(),
            "error": error,
            "attempt": attempts,
        })
        await _set_state(**_shared_snapshot(shared))

        if attempts > self.MAX_REFORMAT:
            shared["_error"] = f"PydanticAFValidation: max reformat attempts exceeded: {error}"
            return "error"

        return "reformat"