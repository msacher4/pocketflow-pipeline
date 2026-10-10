import asyncio
import copy
import time
import logging
from datetime import datetime, timezone

log = logging.getLogger("pocketflow-pipeline")

_CACHED_STATE = {"pipeline_running": False, "current_step": "idle", "steps": []}
_CACHED_STATE_LOCK = asyncio.Lock()
_CURRENT_SHARED = None
_CACHED_TRACES = {}
_CACHED_TRACES_LOCK = asyncio.Lock()
_HISTORY = []
_HISTORY_LOCK = asyncio.Lock()

_pending_validations: dict[str, dict] = {}

_AUTO_APPROVE = False


def set_auto_approve(enabled: bool) -> None:
    """Mode test : toutes les validations Telegram se résolvent d'elles-mêmes."""
    global _AUTO_APPROVE
    _AUTO_APPROVE = enabled
    log.info("[tg] auto-approve %s", "ON" if enabled else "OFF")


def is_auto_approve() -> bool:
    return _AUTO_APPROVE

async def _set_state(**kwargs):
    async with _CACHED_STATE_LOCK:
        _CACHED_STATE.update(kwargs)

async def _get_state():
    async with _CACHED_STATE_LOCK:
        return copy.deepcopy(_CACHED_STATE)

def _shared_snapshot(shared: dict, running: bool = True) -> dict:
    return {
        "pipeline_running": running,
        "current_step": shared.get("_current_step", "unknown"),
        "pipeline_id": shared.get("pipeline_id"),
        "started_at": shared.get("started_at"),
        "finished_at": shared.get("finished_at"),
        "pipeline_result": shared.get("pipeline_result"),
        "error": shared.get("_error"),
        "steps": list(shared.get("steps", [])),
    }

async def _push_history(entry):
    async with _HISTORY_LOCK:
        _HISTORY.append(entry)
        if len(_HISTORY) > 20:
            _HISTORY.pop(0)

async def _get_history():
    async with _HISTORY_LOCK:
        return list(reversed(_HISTORY))[:20]

async def _set_traces(traces: dict):
    async with _CACHED_TRACES_LOCK:
        _CACHED_TRACES.clear()
        _CACHED_TRACES.update(copy.deepcopy(traces))

async def _get_traces():
    async with _CACHED_TRACES_LOCK:
        return copy.deepcopy(_CACHED_TRACES)

def _register_validation(vid: str):
    event = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    entry = {"event": event, "result": None, "created": time.time(), "message_ids": [], "loop": loop}
    _pending_validations[vid] = entry
    if _AUTO_APPROVE:
        # Mode test : on résout immédiatement, sans attendre les boutons Telegram.
        result = "approve"
        if vid.startswith("bs_"):
            # Le brainstorm attend "brainstorm_validate" ; n'importe quelle autre
            # valeur le ferait boucler à l'infini dans while True.
            result = "brainstorm_validate"
        entry["result"] = result
        log.info("[tg] auto-approve resolved %s -> %s", vid, result)
        if loop and loop.is_running():
            loop.call_soon_threadsafe(event.set)
        else:
            event.set()
    return event

def _attach_message(vid: str, msg_id: int):
    entry = _pending_validations.get(vid)
    if entry:
        entry["message_ids"].append(msg_id)

def _resolve_validation(vid: str, result: str) -> bool:
    entry = _pending_validations.get(vid)
    if not entry or entry["event"].is_set():
        return False
    entry["result"] = result
    loop = entry.get("loop")
    if loop and loop.is_running():
        loop.call_soon_threadsafe(entry["event"].set)
    else:
        entry["event"].set()
    return True

def _resolve_validation_by_message(message_id: int, text: str) -> bool:
    """Resolve a pending validation by matching reply_to_message_id."""
    for vid, entry in _pending_validations.items():
        if entry["event"].is_set():
            continue
        if message_id in entry.get("message_ids", []):
            entry["result"] = f"feedback:{text}"
            loop = entry.get("loop")
            if loop and loop.is_running():
                loop.call_soon_threadsafe(entry["event"].set)
            else:
                entry["event"].set()
            return True
    return False

async def _cleanup_stale_validations():
    while True:
        now = time.time()
        stale = [k for k, v in _pending_validations.items()
                 if v["event"].is_set() or now - v["created"] > 3600]
        for k in stale:
            _pending_validations.pop(k, None)
        await asyncio.sleep(60)


_sub_shared_stores: dict[str, dict] = {}
_sub_shared_stores_lock = asyncio.Lock()

def _save_sub_shared(name: str, sub: dict):
    _sub_shared_stores[name] = copy.deepcopy(sub)

def _get_sub_shared(name: str) -> dict:
    return copy.deepcopy(_sub_shared_stores.get(name, {}))


# Sub-shareds "live" : référence (sans deepcopy) vers le dict `sub` d'un
# SubFlowNode en cours d'exécution. Permet d'exposer article + thinking_agent
# en temps réel pendant le run (ex: capture manuelle d'une VO générée à la
# main), là où `_save_sub_shared` n'est appelé qu'à la fin du sous-flow.
_LIVE_SUB_SHARED: dict[str, dict] = {}


def _set_live_sub(name: str, sub: dict) -> None:
    _LIVE_SUB_SHARED[name] = sub


def _get_live_sub(name: str) -> dict:
    return _LIVE_SUB_SHARED.get(name, {})
