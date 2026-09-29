import helpers.force_ipv4  # noqa: F401 — force IPv4 DNS resolution

import asyncio
import copy
import json
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")

from pocketflow import AsyncFlow, AsyncNode

from config import DAEMON_PORT, SCHEDULE_INTERVAL_HOURS


ALT_RETRY_MAX = 3
from helpers.state import (
    _CURRENT_SHARED,
    _get_state, _set_state, _shared_snapshot,
    _get_history, _push_history,
    _get_traces,
    _resolve_validation, _resolve_validation_by_message, _cleanup_stale_validations,
    _get_sub_shared,
)
from helpers.send_telegram import send_telegram
from helpers.call_llm import log

from nodes.base import ResetNode, SubFlowNode, ValidationSubFlowNode, RouteChoiceNode
from nodes.video_analysis import build_video_analysis_flow
from nodes.viralfinder import build_viralfinder_flow
from nodes.scriptwriter import build_scriptwriter_flow, build_alt_scriptwriter_flow
from nodes.actufinder import build_actufinder_flow
from nodes.assetfinder import build_assetfinder_flow, build_assetfinder_alt_flow
from nodes.videoeditor import build_videoeditor_flow

_PIPELINE_TASK: asyncio.Task | None = None
_PENDING_TOPIC: str | None = None
_PENDING_ROUTE: str = "normal"


class AltRetryGate(AsyncNode):
    """Limite le rebouclage ActuFinder quand un run alt échoue faute d'image réelle.

    Retourne "retry" tant que ALT_RETRY_MAX n'est pas atteint, sinon "give_up"
    (arrêt propre du pipeline sans re-sélectionner un énième article)."""

    async def prep_async(self, shared):
        return shared

    async def exec_async(self, shared):
        n = int(shared.get("_alt_retry_count", 0)) + 1
        shared["_alt_retry_count"] = n
        return "retry" if n <= ALT_RETRY_MAX else "give_up"

    async def post_async(self, shared, prep, exec):
        await _set_state(**_shared_snapshot(shared))
        return exec


def build_flow() -> AsyncFlow:
    reset = ResetNode()
    router = RouteChoiceNode()

    # Chemin normal : ViralFinder → VideoAnalysis → ScriptWriter
    vf = ValidationSubFlowNode("viralfinder", build_viralfinder_flow,
                               ["topic", "video_feedback"],
                               ["selected_video"])
    video_analysis = SubFlowNode("video_analysis", build_video_analysis_flow,
                                 ["selected_video"],
                                 ["video_analysis"])
    sw = ValidationSubFlowNode("scriptwriter", build_scriptwriter_flow,
                               ["topic", "selected_video", "video_analysis", "script_feedback"],
                               ["script"])

    # Chemin alternatif : Actufinder → ScriptWriter (alt)
    # ValidationSubFlowNode → propage l'action interne : "approve" (news validée)
    # continue vers sw_alt, "abort" (aucune news après MAX_RETRIES) arrête le pipeline.
    actufinder = ValidationSubFlowNode("actufinder", build_actufinder_flow,
                                       ["topic", "news_feedback"],
                                       ["selected_article"])
    sw_alt = ValidationSubFlowNode("scriptwriter_alt", build_alt_scriptwriter_flow,
                                   ["topic", "selected_article", "script_feedback"],
                                   ["script"])

    # Convergence : les deux chemins arrivent ici
    af = ValidationSubFlowNode("assetfinder", build_assetfinder_flow,
                               ["topic", "script"],
                               ["assets", "montage_structure"])
    af_alt = ValidationSubFlowNode("assetfinder_alt", build_assetfinder_alt_flow,
                                   ["topic", "script", "selected_article"],
                                   ["assets", "montage_structure"])
    ve = SubFlowNode("videoeditor", build_videoeditor_flow,
                     ["topic", "script", "assets", "generated_videos", "downloaded_audio", "montage_structure"],
                     ["video_result"])

    # Rebouclage limité : si un run alt échoue faute d'image réelle (decision 1.a),
    # on re-sélectionne un autre article via l'ActuFinder (retry), puis on s'arrête.
    alt_retry = AltRetryGate()

    reset >> router

    # Branchement conditionnel via Telegram
    router - "normal" >> vf >> video_analysis >> sw
    router - "alt"    >> actufinder
    actufinder - "approve" >> sw_alt

    # Les deux chemins convergent vers AssetFinder (normal vs alt)
    sw     - "approve" >> af
    sw_alt - "approve" >> af_alt

    af >> ve
    af_alt >> ve
    af_alt - "retry_no_image" >> alt_retry
    alt_retry - "retry" >> actufinder

    sw     - "scriptwriter" >> sw
    sw_alt - "scriptwriter_alt" >> sw_alt

    return AsyncFlow(start=reset)


def build_sw_validate_debug_flow() -> AsyncFlow:
    """Segment prod : scriptwriter avec validation interne, pour test isolé sur Telegram."""
    sw = SubFlowNode("scriptwriter", build_scriptwriter_flow,
                     ["topic", "selected_video", "video_analysis", "script_feedback"],
                     ["script"])
    return AsyncFlow(start=sw)


def build_sw_alt_validate_debug_flow() -> AsyncFlow:
    """Test isolé du scriptwriter alt (Thinking Agent + Brainstorm + ScriptGen)."""
    sw_alt = SubFlowNode("scriptwriter_alt", build_alt_scriptwriter_flow,
                         ["topic", "selected_article", "script_feedback"],
                         ["script"])
    return AsyncFlow(start=sw_alt)


def build_vf_validate_debug_flow() -> AsyncFlow:
    """Segment prod : viralfinder en boucle (recherche -> sélection -> validation avec feedback)."""
    vf = ValidationSubFlowNode("viralfinder", build_viralfinder_flow,
                               ["topic"],
                               ["selected_video"])
    return AsyncFlow(start=vf)


def build_af_validate_debug_flow() -> AsyncFlow:
    """Segment debug : validate_af (assets existants) -> boucle i2v (image puis vidéo).

    Test isolé du bouton 🎨 Regen I2V sans régénération T2V/sd-cli.
    NB: cliquer 🔄 Regénérer termine le test (branche de génération T2V absente).
    """
    from nodes.validation import build_af_validation_flow
    from nodes.assetfinder.combined_cleanup import CombinedCleanup
    from nodes.assetfinder.sdcpp_i2v_generator import SDCppI2VNode
    from nodes.assetfinder.comfyui_image_generator import ComfyUIImageGenerator

    validate = ValidationSubFlowNode(
        "validate_af", build_af_validation_flow,
        inputs=["topic", "script", "assets", "_feedback_attempts", "generated_videos", "downloaded_audio",
                "generated_images", "_pending_reject_queue", "_rejected_slot", "_rejected_slot_label", "_rejected_slot_type",
                "_slots_to_regenerate", "_video_action", "_i2v_phase", "_i2v_image_prompt"],
        outputs=["assets", "_feedback_attempts", "af_feedback", "user_feedback", "generated_videos", "downloaded_audio",
                 "generated_images", "_pending_reject_queue", "_rejected_slot", "_rejected_slot_label", "_rejected_slot_type",
                 "_slots_to_regenerate", "_video_action", "_i2v_phase", "_i2v_image_prompt"],
    )
    i2v_cleanup = CombinedCleanup(step="i2v_cleanup")
    i2v_video_cleanup = CombinedCleanup(step="i2v_video_cleanup")
    image_gen = ComfyUIImageGenerator()
    sdcpp_i2v = SDCppI2VNode()

    validate - "i2v" >> i2v_cleanup
    validate - "i2v_video" >> i2v_video_cleanup
    i2v_cleanup - "i2v" >> image_gen
    i2v_video_cleanup - "i2v_video" >> sdcpp_i2v
    image_gen - "default" >> validate
    sdcpp_i2v - "regen_done" >> validate

    return AsyncFlow(start=validate)


def build_af_alt_validate_debug_flow() -> AsyncFlow:
    """Test isolé du flow AssetFinder complet du chemin ALT.

    Va de l'AssetPlannerAlt (parse du script → blueprint + 2 I2V marqués)
    jusqu'à la validation finale Telegram des assets (validate_af), en passant
    par RealCharacterImage (images réelles Danbooru), RewriteI2VPrompt, montage,
    T2V (sd-cli), etc. Script + selected_article injectés via shared."""
    return build_assetfinder_alt_flow()


async def run_af_alt_validate_debug():
    """Test isolé du flow AssetFinder alt complet avec un script injecté."""
    global _CURRENT_SHARED
    from pathlib import Path
    script_path = Path(__file__).parent / "tests" / "debug_alt_script.txt"
    script = script_path.read_text() if script_path.is_file() else "Script de test alt."

    shared = {
        "topic": "anime",
        "script": script,
        "selected_article": {
            "title": "Fate/EXTRA Record: Saber Alter Revealed as Playable Servant",
            "source": "Anime News Network",
            "url": "https://www.animenewsnetwork.com/news/2026-09-10/test",
            "synthesis": "Type-Moon has officially revealed Saber Alter (Artoria Pendragon Alter) as a playable servant in the upcoming Fate/EXTRA Record remake. The announcement was made during a special livestream, showing new gameplay footage and character designs. Saber Alter, originally from Fate/stay night, is a fan-favorite dark version of the iconic Artoria Pendragon. The remake promises updated graphics, new story routes, and reimagined battle systems. Fans have been eagerly awaiting this announcement since the game was first teased in 2024.",
            "character": {
                "name": "Saber Alter",
                "franchise": "Fate",
            },
        },
        "pipeline_id": f"debug-af-alt-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "steps": [],
        "_traces": {},
    }
    _CURRENT_SHARED = shared
    await _set_state(pipeline_running=True, current_step="starting", steps=[])
    try:
        flow = build_af_alt_validate_debug_flow()
        action = await flow.run_async(shared)
        log.info(f"AF alt validate debug done: action={action}, error={shared.get('_error')}")
    except Exception as e:
        log.error(f"AF alt validate debug failed: {e}", exc_info=True)
        shared["_error"] = f"{type(e).__name__}: {e}"
    finally:
        await _set_state(**_shared_snapshot(shared, running=False))


async def run_pipeline_once():
    global _CURRENT_SHARED, _PENDING_TOPIC, _PENDING_ROUTE
    shared = {}
    if _PENDING_TOPIC:
        shared["topic"] = _PENDING_TOPIC
        _PENDING_TOPIC = None
    shared["route"] = _PENDING_ROUTE
    _PENDING_ROUTE = "normal"
    _CURRENT_SHARED = shared
    await _set_state(pipeline_running=True, current_step="starting", steps=[])

    try:
        flow = build_flow()
        action = await flow.run_async(shared)
        await _push_history({
            "pipeline_id": shared.get("pipeline_id", "?"),
            "started_at": shared.get("started_at"),
            "finished_at": shared.get("finished_at"),
            "result": "success" if shared.get("pipeline_result") == "success" else "partial",
            "steps": list(shared.get("steps", [])),
        })
        log.info(f"Pipeline completed: {shared.get('pipeline_result', 'partial')}")
    except asyncio.CancelledError:
        log.warning("Pipeline cancelled by user")
        shared["_current_step"] = "cancelled"
        shared["finished_at"] = datetime.now(timezone.utc).isoformat()
        await _push_history({
            "pipeline_id": shared.get("pipeline_id", "?"),
            "started_at": shared.get("started_at"),
            "finished_at": shared["finished_at"],
            "result": "cancelled",
            "steps": list(shared.get("steps", [])),
        })
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__}"
        log.error(f"Pipeline failed: {err_msg}", exc_info=True)
        shared["_error"] = err_msg
        shared["pipeline_result"] = "error"
        await _push_history({
            "pipeline_id": shared.get("pipeline_id", "?"),
            "started_at": shared.get("started_at"),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "result": "error",
            "error": err_msg,
            "steps": list(shared.get("steps", [])),
        })
    finally:
        await _set_state(**_shared_snapshot(shared, running=False))


async def run_sw_validate_debug():
    global _CURRENT_SHARED
    shared = {
        "topic": "motivation fitness",
        "selected_video": {
            "url": "https://www.youtube.com/watch?v=test",
            "description": "Vidéo de motivation sur la musculation et la discipline quotidienne.",
        },
        "video_analysis": {
            "summary": "Séquence de squats et pompes, rythme dynamique.",
            "duration_s": 45,
        },
        "pipeline_id": f"debug-sw-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "steps": [],
        "_traces": {},
    }
    _CURRENT_SHARED = shared
    await _set_state(pipeline_running=True, current_step="starting", steps=[])
    try:
        flow = build_sw_validate_debug_flow()
        action = await flow.run_async(shared)
        log.info(f"SW validate debug done: action={action}, error={shared.get('_error')}")
    except Exception as e:
        log.error(f"SW validate debug failed: {e}", exc_info=True)
        shared["_error"] = f"{type(e).__name__}: {e}"
    finally:
        await _set_state(**_shared_snapshot(shared, running=False))


async def run_sw_alt_validate_debug():
    global _CURRENT_SHARED
    shared = {
        "topic": "anime",
        "selected_article": {
            "title": "Fate/EXTRA Record: Saber Alter Revealed as Playable Servant",
            "source": "Anime News Network",
            "url": "https://www.animenewsnetwork.com/news/2026-09-10/test",
            "synthesis": "Type-Moon has officially revealed Saber Alter (Artoria Pendragon Alter) as a playable servant in the upcoming Fate/EXTRA Record remake. The announcement was made during a special livestream, showing new gameplay footage and character designs. Saber Alter, originally from Fate/stay night, is a fan-favorite dark version of the iconic Artoria Pendragon. The remake promises updated graphics, new story routes, and reimagined battle systems. Fans have been eagerly awaiting this announcement since the game was first teased in 2024.",
            "character": {
                "name": "Saber Alter",
                "franchise": "Fate",
            },
        },
        "pipeline_id": f"debug-sw-alt-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "steps": [],
        "_traces": {},
    }
    _CURRENT_SHARED = shared
    await _set_state(pipeline_running=True, current_step="starting", steps=[])
    try:
        flow = build_sw_alt_validate_debug_flow()
        action = await flow.run_async(shared)
        log.info(f"SW alt validate debug done: action={action}, error={shared.get('_error')}")
    except Exception as e:
        log.error(f"SW alt validate debug failed: {e}", exc_info=True)
        shared["_error"] = f"{type(e).__name__}: {e}"
    finally:
        await _set_state(**_shared_snapshot(shared, running=False))


async def run_vf_validate_debug():
    global _CURRENT_SHARED
    shared = {
        "topic": "motivation fitness",
        "pipeline_id": f"debug-vf-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "steps": [],
        "_traces": {},
        "_search_attempts": 0,
    }
    _CURRENT_SHARED = shared
    await _set_state(pipeline_running=True, current_step="starting", steps=[])
    try:
        flow = build_vf_validate_debug_flow()
        action = await flow.run_async(shared)
        log.info(f"VF validate debug done: action={action}, error={shared.get('_error')}")
    except Exception as e:
        log.error(f"VF validate debug failed: {e}", exc_info=True)
        shared["_error"] = f"{type(e).__name__}: {e}"
    finally:
        await _set_state(**_shared_snapshot(shared, running=False))


async def run_af_validate_debug():
    """Test isolé du bouton 🎨 ControlNet sur Telegram avec des assets déjà générés."""
    global _CURRENT_SHARED
    dl = "/media/marcs/Linux_Apps/Projets_AI/pocketflow_pipeline/downloads/test-af-full-1787494360352"
    shared = {
        "topic": "test controlnet",
        "script": "Script de test pour la correction ControlNet.",
        "pipeline_id": f"debug-cn-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "steps": [],
        "_traces": {},
        "asset_blueprint": {"slots": [
            # Prompts = cible de détection SAM2/GroundingDino. À ajuster selon le contenu réel des clips.
            {"id": 1, "prompt": "a person"},
            {"id": 2, "prompt": "a person"},
        ]},
        "generated_videos": [
            {"slot_id": 1, "video_path": f"{dl}/clip_1.mp4", "content": "Clip test 1", "duration_s": 5, "section": "hook"},
            {"slot_id": 2, "video_path": f"{dl}/clip_2.mp4", "content": "Clip test 2", "duration_s": 5, "section": "demo"},
        ],
        "downloaded_audio": [
            {"slot_id": "a1", "type": "music", "path": f"{dl}/music_a1.mp3", "mood": "test", "section": "music"},
            {"slot_id": "v1", "type": "voiceover", "path": f"{dl}/vo_v1.mp3", "text": "Voix test 1", "section": "vo"},
            {"slot_id": "v2", "type": "voiceover", "path": f"{dl}/vo_v2.mp3", "text": "Voix test 2", "section": "vo"},
        ],
    }
    _CURRENT_SHARED = shared
    await _set_state(pipeline_running=True, current_step="starting", steps=[])
    try:
        flow = build_af_validate_debug_flow()
        action = await flow.run_async(shared)
        log.info(f"AF validate debug done: action={action}, error={shared.get('_error')}")
    except Exception as e:
        log.error(f"AF validate debug failed: {e}", exc_info=True)
        shared["_error"] = f"{type(e).__name__}: {e}"
    finally:
        await _set_state(**_shared_snapshot(shared, running=False))


async def pipeline_daemon():
    log.info(f"Pipeline daemon started (interval={SCHEDULE_INTERVAL_HOURS}h)")
    asyncio.create_task(_cleanup_stale_validations())
    while True:
        state = await _get_state()
        if state.get("pipeline_running"):
            log.info("Pipeline already running, skipping cycle")
            await asyncio.sleep(60)
            continue
        log.info("Starting pipeline cycle")
        await run_pipeline_once()
        log.info(f"Pipeline cycle done, sleeping {SCHEDULE_INTERVAL_HOURS}h")
        await asyncio.sleep(SCHEDULE_INTERVAL_HOURS * 3600)


from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="PocketFlow Pipeline Daemon")


@app.get("/api/state")
async def get_state():
    return await _get_state()


HIDDEN_KEYS = frozenset({
    "_current_step", "_error", "_traces",
    "pipeline_id", "started_at", "finished_at",
    "pipeline_result", "steps",
    "step", "status", "ts",
})


@app.get("/api/state/shared")
async def get_shared():
    s = _CURRENT_SHARED
    if s is None:
        return {}
    safe = {}
    for k, v in s.items():
        if k in HIDDEN_KEYS:
            continue
        if isinstance(v, str) and len(v) > 2000:
            safe[k] = v[:2000] + f"... [{len(v)} chars]"
        else:
            safe[k] = v
    return safe


@app.get("/api/state/history")
async def get_history():
    return await _get_history()


@app.get("/api/flow")
async def get_flow():
    """Sérialise le graphe PocketFlow (nodes + edges) pour la vue Svelte."""
    from helpers.flow_serializer import serialize_flow
    return serialize_flow(build_flow())


@app.get("/api/state/sub/{step_name}")
async def get_sub_shared(step_name: str):
    sub = _get_sub_shared(step_name)
    safe = {}
    for k, v in sub.items():
        if k in HIDDEN_KEYS:
            continue
        if isinstance(v, str) and len(v) > 2000:
            safe[k] = v[:2000] + f"... [{len(v)} chars]"
        else:
            safe[k] = v
    return safe


@app.get("/api/state/traces/{step}")
async def get_step_traces(step: str):
    traces = await _get_traces()
    return traces.get(step, {})


@app.post("/api/state/trigger")
async def trigger_pipeline(request: Request):
    global _PIPELINE_TASK, _PENDING_TOPIC, _PENDING_ROUTE
    state = await _get_state()
    if state.get("pipeline_running"):
        return JSONResponse({"status": "error", "message": "Pipeline already running"}, status_code=409)
    try:
        body = await request.json()
        _PENDING_TOPIC = body.get("topic", "")
        route = body.get("route", "normal")
        if route in ("normal", "alt"):
            _PENDING_ROUTE = route
    except Exception:
        _PENDING_TOPIC = ""
    _PIPELINE_TASK = asyncio.create_task(run_pipeline_once())
    return {"status": "ok", "message": "Pipeline triggered"}


@app.post("/api/state/cancel")
async def cancel_pipeline():
    global _PIPELINE_TASK
    task = _PIPELINE_TASK
    if not task or task.done():
        return JSONResponse({"status": "error", "message": "No running pipeline"}, status_code=409)
    task.cancel()
    _PIPELINE_TASK = None
    return {"status": "ok", "message": "Pipeline cancelled"}


@app.post("/api/debug/run-sw-validate")
async def debug_run_sw_validate():
    """Test isolé: scriptwriter -> validate_sw sur Telegram (mock inputs)."""
    global _PIPELINE_TASK
    state = await _get_state()
    if state.get("pipeline_running"):
        return JSONResponse({"status": "error", "message": "Pipeline already running"}, status_code=409)
    _PIPELINE_TASK = asyncio.create_task(run_sw_validate_debug())
    return {"status": "ok", "message": "SW validate debug started"}


@app.post("/api/debug/run-vf-validate")
async def debug_run_vf_validate():
    """Test isolé: viralfinder -> validate_vf sur Telegram (recherche TikHub réelle)."""
    global _PIPELINE_TASK
    state = await _get_state()
    if state.get("pipeline_running"):
        return JSONResponse({"status": "error", "message": "Pipeline already running"}, status_code=409)
    _PIPELINE_TASK = asyncio.create_task(run_vf_validate_debug())
    return {"status": "ok", "message": "VF validate debug started"}


@app.post("/api/debug/run-af-validate")
async def debug_run_af_validate():
    """Test isolé: validate_af (assets existants) -> bouton 🎨 Regen I2V sur Telegram."""
    global _PIPELINE_TASK
    state = await _get_state()
    if state.get("pipeline_running"):
        return JSONResponse({"status": "error", "message": "Pipeline already running"}, status_code=409)
    _PIPELINE_TASK = asyncio.create_task(run_af_validate_debug())
    return {"status": "ok", "message": "AF validate debug started"}


@app.post("/api/debug/run-sw-alt-validate")
async def debug_run_sw_alt_validate():
    """Test isolé: Thinking Agent + Brainstorm + ScriptWriter InfoMissed (article mock)."""
    global _PIPELINE_TASK
    state = await _get_state()
    if state.get("pipeline_running"):
        return JSONResponse({"status": "error", "message": "Pipeline already running"}, status_code=409)
    _PIPELINE_TASK = asyncio.create_task(run_sw_alt_validate_debug())
    return {"status": "ok", "message": "SW alt validate debug started"}


@app.post("/api/debug/run-af-alt-validate")
async def debug_run_af_alt_validate():
    """Test isolé: flow AssetFinder ALT complet (script + selected_article injectés)."""
    global _PIPELINE_TASK
    state = await _get_state()
    if state.get("pipeline_running"):
        return JSONResponse({"status": "error", "message": "Pipeline already running"}, status_code=409)
    _PIPELINE_TASK = asyncio.create_task(run_af_alt_validate_debug())
    return {"status": "ok", "message": "AF alt validate debug started"}


@app.post("/webhook/tg-callback")
async def tg_callback(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    cb = body.get("callback_query") or body
    data = cb.get("data", "")

    if ":" not in data:
        return {"status": "ok"}

    parts = data.split(":")
    action = parts[0]

    if action in ("approve", "reject", "regen", "i2v", "edit", "route_normal", "route_alt"):
        vid = parts[1] if len(parts) > 1 else ""
        slot_id = parts[2] if len(parts) > 2 else ""
        if slot_id:
            ok = _resolve_validation(f"{vid}_{slot_id}", f"{action}:{vid}:{slot_id}")
        else:
            ok = _resolve_validation(vid, action)
        log.info(f"Validation {vid} slot={slot_id}: {action} {'(resolved)' if ok else '(not found)'}")

    elif action == "img_approve":
        vid = parts[1] if len(parts) > 1 else ""
        slot_id = parts[2] if len(parts) > 2 else ""
        ok = _resolve_validation(vid, f"approve:{slot_id}")
        log.info(f"Image approve {vid} slot={slot_id}: {'(resolved)' if ok else '(not found)'}")

    elif action == "img_approve_all":
        vid = parts[1] if len(parts) > 1 else ""
        ok = _resolve_validation(vid, "approve_all")
        log.info(f"Image approve all {vid}: {'(resolved)' if ok else '(not found)'}")

    elif action == "img_reject_all":
        vid = parts[1] if len(parts) > 1 else ""
        ok = _resolve_validation(vid, "reject_all")
        log.info(f"Image reject all {vid}: {'(resolved)' if ok else '(not found)'}")

    elif action == "brainstorm_validate":
        vid = parts[1] if len(parts) > 1 else ""
        ok = _resolve_validation(vid, "brainstorm_validate")
        log.info(f"Brainstorm validate {vid}: {'(resolved)' if ok else '(not found)'}")

    return {"status": "ok"}


@app.post("/webhook/tg-message")
async def tg_message(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)

    message_id = body.get("message_id")
    reply_to = body.get("reply_to_message_id")
    text = body.get("text", "")

    # Commandes admin directes (sans réponse à une validation).
    cmd = (text or "").strip().lower()
    if reply_to is None and cmd in ("/reset_used", "/clear_used", "/reset_articles"):
        try:
            from nodes.actufinder.used_articles import clear_used
            n = clear_used()
            await send_telegram(
                f"🗑️ {n} article(s) déjà traités remis en circulation (used_articles.json vidé)."
            )
        except Exception as e:
            await send_telegram(f"⚠️ Erreur vidage used_articles : {str(e)[:200]}")
        return {"status": "ok"}

    if reply_to and text:
        ok = _resolve_validation_by_message(reply_to, text)
        log.info(f"TG message {message_id} reply_to={reply_to}: {'(resolved)' if ok else '(not found)'}")
    else:
        log.info(f"TG message {message_id}: no reply_to or empty text, ignored")

    return {"status": "ok"}


@app.post("/api/state/reset")
async def reset_state():
    await _set_state(pipeline_running=False, current_step="idle", steps=[])
    return {"status": "ok"}


@app.post("/api/state/used-reset")
async def reset_used_articles():
    """Vide la liste des articles déjà traités (used_articles.json)."""
    from nodes.actufinder.used_articles import clear_used
    n = clear_used()
    return {"status": "ok", "cleared": n, "message": f"{n} article(s) remis en circulation"}


def main():
    import sys
    if "--help" in sys.argv:
        print("Usage: python main.py [options]")
        print("  (no args)    Start HTTP server (manual trigger only)")
        print("  --once       Run pipeline once and exit")
        print("  --daemon     Start HTTP server + auto-schedule loop")
        return

    if "--once" in sys.argv:
        asyncio.run(run_pipeline_once())
        return

    auto_schedule = "--daemon" in sys.argv
    config = uvicorn.Config(app, host="127.0.0.1", port=DAEMON_PORT, log_level="info")
    server = uvicorn.Server(config)

    async def startup():
        if auto_schedule:
            asyncio.create_task(pipeline_daemon())
        await server.serve()

    asyncio.run(startup())


if __name__ == "__main__":
    main()
