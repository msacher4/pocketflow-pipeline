"""Node terminal du chemin approve alt : capture le script validé dans le dataset."""
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.dataset import record_validated_script
from helpers.state import _set_state, _shared_snapshot

log = logging.getLogger("pocketflow-pipeline")


class DatasetCollectorNode(AsyncNode):
    """Capture le script approuvé dans le dataset d'entraînement (JSONL).

    Inséré sur l'arête `validate - "approve"` du ScriptWriter alt : c'est le
    point de convergence unique de tous les approve alt (bouton direct,
    feedback, ✏️ edit re-validé, ⚡ boost). Le sub-shared contient à la fois le
    contexte d'entrée (selected_article, thinking_agent) et le script validé,
    ce que le shared parent ne propage pas. Retourne toujours 'approve' pour
    laisser le pipeline continuer vers AssetFinder."""

    def __init__(self, step_name: str = "dataset_collector"):
        super().__init__(max_retries=1, wait=0)
        self.step_name = step_name

    async def prep_async(self, shared):
        shared["_current_step"] = self.step_name
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        source = shared.get("_script_source", "approve")
        try:
            record_validated_script(shared, source)
        except Exception as e:  # la capture ne doit jamais casser le pipeline
            log.warning(f"[dataset] capture échouée: {type(e).__name__}: {e}")
        return "approve"

    async def post_async(self, shared, prep, exec):
        shared["_current_step"] = f"{self.step_name}_done"
        shared.setdefault("steps", []).append({
            "step": self.step_name, "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        await _set_state(**_shared_snapshot(shared))
        return "approve"
