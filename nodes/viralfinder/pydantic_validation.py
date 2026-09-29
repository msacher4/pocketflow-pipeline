import json
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, field_validator
from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _save_sub_shared

log = logging.getLogger("pocketflow-pipeline")


class SelectedVideo(BaseModel):
    url: str
    description: str

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("url must not be empty")
        return v.strip()

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("description must not be empty")
        return v.strip()


class PydanticValidationNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "pydantic_validation"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        video_data = shared.get("selected_video", {})
        try:
            SelectedVideo(**video_data)
            return {"valid": True}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    async def post_async(self, shared, prep, exec):
        if exec["valid"]:
            shared["_current_step"] = "pydantic_validation_done"
            shared["steps"].append({
                "step": "pydantic_validation",
                "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": json.dumps(shared.get("selected_video", {}), ensure_ascii=False)[:2000],
            })
            await _set_state(**_shared_snapshot(shared))
            _save_sub_shared("viralfinder", shared)
            return "default"

        error = exec["error"]
        shared["_reformat_error"] = error
        shared["_reformat_attempts"] = shared.get("_reformat_attempts", 0) + 1
        attempts = shared["_reformat_attempts"]

        shared["_current_step"] = "pydantic_validation_reformat"
        shared["steps"].append({
            "step": "pydantic_validation",
            "status": "reformat",
            "ts": datetime.now(timezone.utc).isoformat(),
            "error": error,
            "attempt": attempts,
        })
        await _set_state(**_shared_snapshot(shared))

        if attempts > 3:
            shared["_error"] = f"PydanticValidation: max reformat attempts exceeded: {error}"
            return "error"

        return "reformat"
