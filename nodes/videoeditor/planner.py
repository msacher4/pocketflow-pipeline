import json
import logging
import os
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator
from pocketflow import AsyncNode

from config import LLM_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, LLMJSONQuoteError

log = logging.getLogger("pocketflow-pipeline")


class MontageSegment(BaseModel):
    index: int = Field(ge=1)
    section: str
    file: str
    start_s: float = Field(ge=0)
    end_s: float
    audio_ref: str = ""

    @field_validator("end_s")
    @classmethod
    def end_after_start(cls, v, info):
        start = info.data.get("start_s", 0)
        if v <= start:
            raise ValueError("end_s must be > start_s")
        return v


class AudioTrack(BaseModel):
    ref: str
    path: str
    start_s: float = Field(default=0, ge=0)          # offset dans la timeline finale
    trim_start_s: float = Field(default=0, ge=0)     # offset dans la piste source
    trim_dur_s: float = Field(default=0, ge=0)
    volume: float = Field(default=0.9, gt=0)


class Subtitle(BaseModel):
    text: str
    start_s: float = Field(ge=0)
    end_s: float

    @field_validator("end_s")
    @classmethod
    def end_after_start(cls, v, info):
        start = info.data.get("start_s", 0)
        if v <= start:
            raise ValueError("end_s must be > start_s")
        return v


class MontageStructure(BaseModel):
    segments: list[MontageSegment] = Field(min_length=1)
    audio_tracks: list[AudioTrack] = Field(default_factory=list)
    subtitles: list[Subtitle] = Field(default_factory=list)
    title: str = ""

    @field_validator("segments")
    @classmethod
    def segments_unique_index(cls, v):
        idxs = [s.index for s in v]
        if len(set(idxs)) != len(idxs):
            raise ValueError("segment indexes must be unique")
        return v


class VideoEditorPlannerNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    @staticmethod
    def _normalize_timestamps(decision: dict, videos: list[dict]) -> dict:
        """Borne chaque segment aux durées réelles de son clip (les timestamps du
        plan markdown sont cumulés dans la vidéo finale, PAS relatifs au clip)."""
        duration_by_path: dict[str, float] = {}
        for v in videos:
            path = v.get("video_path", "")
            dur = float(v.get("duration_s", 0) or 0)
            if path:
                duration_by_path[path] = dur
        segments = decision.get("segments", [])
        for seg in segments:
            dur = duration_by_path.get(seg.get("file", ""), 0)
            if dur > 0:
                seg["start_s"] = 0.0
                seg["end_s"] = round(dur, 3)
            else:
                seg["start_s"] = float(seg.get("start_s", 0) or 0)
                end = float(seg.get("end_s", 0) or 0)
                if end <= seg["start_s"]:
                    end = seg["start_s"] + 1.0
                seg["end_s"] = round(end, 3)
        decision["segments"] = segments
        return decision

    @staticmethod
    def _resolve_path(path: str) -> str:
        """Résout un chemin potentiellement relatif (le LLM renvoie parfois
        /downloads/... ou downloads/...) vers un chemin absolu existant."""
        if not path:
            return path
        p = os.path.abspath(path)
        if os.path.exists(p):
            return p
        stripped = path.lstrip("/")
        p2 = os.path.abspath(stripped)
        if os.path.exists(p2):
            return p2
        return path

    def _resolve_structure(self, decision: dict, videos: list[dict]) -> dict:
        # Index des clips réellement générés par slot_id (1-based, cohérent avec
        # les segments du plan). On garde la dernière vidéo valide par slot.
        clips_by_slot: dict[int, str] = {}
        for v in videos:
            path = v.get("video_path", "")
            if not path:
                continue
            sid = v.get("slot_id")
            if isinstance(sid, int) and sid >= 1:
                clips_by_slot[sid] = path
        # Ordre des clips pour un match par position ordinale en dernier recours.
        ordered = [v["video_path"] for v in videos if v.get("video_path") and v.get("slot_id")]

        segments = decision.get("segments", [])
        for i, seg in enumerate(segments):
            file = seg.get("file", "")
            resolved = self._resolve_path(file)
            if os.path.exists(resolved):
                seg["file"] = resolved
                continue
            # Le chemin du LLM n'existe pas (ex: placeholder halluciné) : on
            # rattache le clip correspondant à partir des vidéos réellement
            # générées, par slot_id puis par position.
            mapped = clips_by_slot.get(seg.get("index"))
            if not mapped and ordered:
                mapped = ordered[i] if i < len(ordered) else ordered[-1]
            if mapped and os.path.exists(mapped):
                log.warning(
                    f"VideoEditorPlanner: chemin LLM '{file}' inexistant, "
                    f"rattaché au clip réel '{mapped}' (segment #{seg.get('index')})"
                )
                seg["file"] = mapped
        for tr in decision.get("audio_tracks", []):
            tr["path"] = self._resolve_path(tr.get("path", ""))
        return decision

    async def prep_async(self, shared):
        shared["_current_step"] = "videoeditor_planner"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        existing = shared.get("montage_structure")
        if isinstance(existing, dict) and existing.get("segments"):
            log.info(
                f"VideoEditorPlanner: montage_structure déjà fournie par le "
                f"MontageLinker ({len(existing['segments'])} segments), skip LLM"
            )
            return json.dumps(existing, ensure_ascii=False)

        soul = load_soul("videoeditor")
        videos = shared.get("generated_videos", [])
        audio = shared.get("downloaded_audio", [])
        plan = shared.get("assets", "")
        retry_hint = shared.get("_llm_retry_hint", "")
        ctx = (
            f"Convertit le plan de montage en structure exploitable.\n"
            f"Thème: {shared.get('topic', '')}\n"
            f"Script:\n{shared.get('script', '')[:2000]}\n\n"
            f"Plan de montage (markdown, peut être imprécis):\n{plan[:4000]}\n\n"
            f"Clips vidéo générés (durées réelles en `duration_s`):\n"
            f"{json.dumps(videos, ensure_ascii=False)[:4000]}\n\n"
            f"Pistes audio téléchargées (via `path`):\n"
            f"{json.dumps(audio, ensure_ascii=False)[:2000]}\n"
        )
        if retry_hint:
            ctx += f"\n--- REPONSE PRECEDENTE INVALIDE ---\n{retry_hint}\n"
        ctx += (
            f"\nRetourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Les timestamps de chaque segment doivent être BORNES par la durée réelle "
            f"du clip choisi (durée max = duration_s du clip). "
            f"Respecte l'ordre narratif du plan (Hook -> Body -> CTA).\n"
            f"N'utilise JAMAIS de guillemets doubles (\" ) dans les valeurs : "
            f"remplace-les par des apostrophes simples (').\n"
            f"Format : "
            f'{{"title": "...", "segments": [{{"index": 1, "section": "Hook", '
            f'"file": "chemin_absolu_clip", "start_s": 0, "end_s": 4.06, '
            f'"audio_ref": "a1"}}], "audio_tracks": [{{"ref": "a1", '
            f'"path": "chemin_audio", "trim_start_s": 0, "trim_dur_s": 8}}]}}'
        )
        resp = await call_llm(LLM_MODEL, soul, ctx)
        _trace_llm(shared, "videoeditor_planner", "exec", LLM_MODEL, soul, ctx, resp)
        try:
            decision = _extract_json(resp)
            decision = self._resolve_structure(decision, videos)
            decision = self._normalize_timestamps(decision, videos)
            MontageStructure(**decision)
        except LLMJSONQuoteError as e:
            shared["_llm_retry_hint"] = (
                "Ta réponse contenait des guillemets doubles non échappés, "
                "le JSON était donc invalide. Réécris en remplaçant TOUS les "
                "guillemets doubles par des apostrophes simples (')."
            )
            log.warning(f"VideoEditorPlanner retry: quotes ({str(e)[:120]})")
            raise
        except ValueError as e:
            shared["_llm_retry_hint"] = f"Ta réponse n'était pas un JSON exploitable : {e}"
            log.warning(f"VideoEditorPlanner retry: parse/validation error ({str(e)[:120]})")
            raise
        shared["_llm_retry_hint"] = ""
        n_seg = len(decision.get("segments", []))
        n_audio = len(decision.get("audio_tracks", []))
        log.info(f"VideoEditorPlanner -> {n_seg} segments, {n_audio} audio tracks")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("VideoEditorPlanner POST -> exec is not valid JSON")
            shared["_current_step"] = "videoeditor_planner_error"
            shared["_error"] = "VideoEditorPlanner: exec is not valid JSON"
            shared["steps"].append({
                "step": "videoeditor_planner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("assets", "")[:500],
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VideoEditorPlanner aborted: exec is not valid JSON")

        segments = decision.get("segments", [])
        if not segments:
            log.error("VideoEditorPlanner POST -> no segments")
            shared["_current_step"] = "videoeditor_planner_error"
            shared["_error"] = "VideoEditorPlanner: no segments"
            shared["steps"].append({
                "step": "videoeditor_planner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("assets", "")[:500],
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VideoEditorPlanner aborted: no segments")

        shared["montage_structure"] = decision
        shared["_current_step"] = "videoeditor_planner_done"
        shared["steps"].append({
            "step": "videoeditor_planner", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("assets", "")[:500],
            "output": f"{len(segments)} segments, {len(decision.get('audio_tracks', []))} audio tracks",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
