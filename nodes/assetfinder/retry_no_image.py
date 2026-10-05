import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces

log = logging.getLogger("pocketflow-pipeline")


class RetryNoImage(AsyncNode):
    """Termine le sous-flux alt en "retry_no_image" pour reboucler sur l'ActuFinder.

    RealCharacterImageNode renvoie "retry_no_image" quand un slot I2V n'a pas
    d'image reelle (personnage introuvable via icrawler). Sans successeur
    cable, le sous-flux s'arretait sur :

        Flow ends: 'retry_no_image' not found in ['default']

    et le run mourait en silence, sans VideoEditor ni montage. Ce noeud est
    donc le point d'arrivee de cette branche : il rend l'action que
    ValidationSubFlowNode (nodes/base.py) propage au parent, ou
    `af_alt - "retry_no_image" >> alt_retry` (main.py) reboucle vers
    l'ActuFinder via AltRetryGate, borne a ALT_RETRY_MAX tentatives.

    On ne pose surtout PAS shared["_error"] ici : ValidationSubFlowNode
    remplace alors l'action par "error" et le rebouclage est perdu.
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=0)
        self.step = "retry_no_image"

    async def prep_async(self, shared):
        return shared

    async def exec_async(self, shared):
        return {"missing": shared.get("_missing_image_slots", []) or []}

    async def post_async(self, shared, prep, exec):
        missing = (exec or {}).get("missing") or []
        log.warning(f"RetryNoImage: slots I2V sans image reelle {missing} -> rebouclage ActuFinder")

        shared["_current_step"] = "retry_no_image"
        shared.setdefault("steps", []).append({
            "step": "retry_no_image", "status": "retry",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"slots I2V sans image reelle: {missing or '?'} -> rebouclage ActuFinder",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "retry_no_image"