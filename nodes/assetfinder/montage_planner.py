import json
import logging
import os
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator
from pocketflow import AsyncNode

from config import LLM_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, LLMJSONQuoteError
from helpers.ffmpeg import ffprobe

log = logging.getLogger("pocketflow-pipeline")

SECTION_ORDER = {"hook": 0, "body": 1, "cta": 2}

# Tolérance sur la durée réelle d'une VO vs longueur du segment. Au-delà, la fin
# de la VO serait coupée au montage : on avertis au log (pas d'échec de la run).
VO_DURATION_TOL = 0.2


def _norm_section(raw: str) -> str:
    key = str(raw or "body").strip().lower().split("(")[0].strip()
    if key in SECTION_ORDER:
        return key
    for s in SECTION_ORDER:
        if s in key:
            return s
    return "body"


def _section_rank(raw: str) -> int:
    return SECTION_ORDER.get(_norm_section(raw), 99)


class LinkSegment(BaseModel):
    index: int = Field(ge=1)
    section: str = "body"
    file: str
    start_s: float = Field(ge=0)
    end_s: float
    audio_ref: str = ""

    @field_validator("end_s")
    @classmethod
    def end_after_start(cls, v, info):
        start = info.data.get("start_s", 0)
        if v <= start:
            raise ValueError("end_s doit être > start_s")
        return v


class LinkAudioTrack(BaseModel):
    ref: str
    path: str
    type: str = "music"
    start_s: float = Field(default=0, ge=0)
    trim_start_s: float = Field(default=0, ge=0)
    trim_dur_s: float = Field(default=0, ge=0)
    volume: float = Field(default=0.9, gt=0, le=1)
    duration_s: float = Field(default=0, ge=0)


class LinkSubtitle(BaseModel):
    text: str
    start_s: float = Field(ge=0)
    end_s: float

    @field_validator("end_s")
    @classmethod
    def end_after_start(cls, v, info):
        start = info.data.get("start_s", 0)
        if v <= start:
            raise ValueError("end_s doit être > start_s")
        return v


class MontageLinkPlan(BaseModel):
    title: str = ""
    segments: list[LinkSegment] = Field(min_length=1)
    audio_tracks: list[LinkAudioTrack] = Field(default_factory=list)
    subtitles: list[LinkSubtitle] = Field(default_factory=list)

    @field_validator("segments")
    @classmethod
    def segments_unique_index(cls, v):
        idxs = [s.index for s in v]
        if len(set(idxs)) != len(idxs):
            raise ValueError("les index de segments doivent être uniques")
        return v

    @model_validator(mode="after")
    def warn_vo_longer_than_segment(self):
        """Avertit (au log) si une VO dure plus longtemps que son segment :
        la fin de la VO serait coupée au montage (trim_dur_s borne la piste)."""
        for tr in self.audio_tracks:
            if tr.type != "voiceover" or tr.duration_s <= 0:
                continue
            over = tr.duration_s - tr.trim_dur_s
            if over > VO_DURATION_TOL:
                log.warning(
                    f"MontagePlanner: VO '{tr.ref}' ({os.path.basename(tr.path)}) "
                    f"dure {tr.duration_s:.2f}s pour un segment de {tr.trim_dur_s:.2f}s — "
                    f"fin de VO coupée ({over:.2f}s au-delà)"
                )
        return self


def _resolve_path(path: str) -> str:
    if not path:
        return ""
    p = os.path.abspath(path)
    if os.path.exists(p):
        return p
    stripped = path.lstrip("/")
    p2 = os.path.abspath(stripped)
    if os.path.exists(p2):
        return p2
    return p


class MontagePlannerNode(AsyncNode):
    """Lie le plan du script aux assets générés, puis reconstruit le plan de
    montage de façon DÉTERMINISTE (ordre du script, chaque clip une fois, durées
    réelles). Le LLM n'apporte que les choix créatifs (audio_ref, SFX, titles).

    Remplace l'ancien couple MontagePlanner (markdown libre) + MontageCritic
    (boucle reformat inefficace) : la validation devient programmatique."""

    def __init__(self):
        super().__init__(max_retries=4, wait=30)

    # ------------------------------------------------------------------ #
    # Post-traitement programmatique
    # ------------------------------------------------------------------ #
    @staticmethod
    def _audio_by_slot(audio: list[dict]) -> dict:
        return {
            str(a.get("slot_id")): a
            for a in audio if a.get("path") and os.path.exists(a.get("path"))
        }

    @staticmethod
    def _ordered_clips(videos: list[dict]) -> list[dict]:
        return sorted(
            [v for v in videos if v.get("video_path")],
            key=lambda v: (_section_rank(v.get("section", "body")), v.get("position", 0)),
        )

    @staticmethod
    def _plan_groups(segments: list[dict]) -> list[dict]:
        """Regroupe les segments consécutifs d'un même plan (plan_index non-None).

        Utile pour les plans multi-assets (ex: I2V 3s + T2V 4s) : leur VO unique
        doit couvrir la somme des segments, pas être dupliquée par segment.
        Les segments sans plan_index (None) ne sont JAMAIS regroupés (un groupe
        chacun) => comportement inchangé pour les scripts sans en-têtes Plan."""
        groups = []
        for seg in segments:
            pi = seg.get("plan_index")
            if groups and pi is not None and pi == groups[-1]["plan_index"]:
                groups[-1]["segments"].append(seg)
                groups[-1]["duration"] += seg["end_s"] - seg["start_s"]
            else:
                groups.append({
                    "plan_index": pi,
                    "segments": [seg],
                    "duration": seg["end_s"] - seg["start_s"],
                })
        return groups

    def _resolve_structure(self, decision: dict, videos: list[dict], audio: list[dict]) -> dict:
        """Normalise les chemins audio du LLM vers les fichiers réels téléchargés."""
        audio_by_slot = self._audio_by_slot(audio)
        by_basename = {
            os.path.basename(a["path"]): a["path"]
            for a in audio if a.get("path")
        }
        for tr in decision.get("audio_tracks", []):
            resolved = _resolve_path(tr.get("path", ""))
            if not os.path.exists(resolved):
                resolved = by_basename.get(os.path.basename(tr.get("path", "")))
            if not resolved:
                candidate = audio_by_slot.get(tr.get("ref", ""))
                resolved = candidate["path"] if candidate else ""
            tr["path"] = resolved or tr.get("path", "")
        return decision

    def _build_segments(self, decision: dict, videos: list[dict]) -> dict:
        """Reconstruit la séquence depuis les clips réels, dans l'ordre du script.

        Chaque clip généré apparaît EXACTEMENT une fois. L'audio_ref reprend le
        choix du LLM quand il est fourni, sinon la VO 'v{slot}' correspondante."""
        clips = self._ordered_clips(videos)
        llm_by_index = {s.get("index"): s for s in decision.get("segments", [])}

        segments = []
        for i, clip in enumerate(clips, start=1):
            dur = float(clip.get("duration_s", 0) or 0)
            llm_seg = llm_by_index.get(clip.get("slot_id"))
            if llm_seg is None:
                llm_seg = llm_by_index.get(i)
            ref = (llm_seg or {}).get("audio_ref") or ""
            if not ref:
                sid = clip.get("slot_id")
                ref = f"v{sid}" if sid is not None and not isinstance(sid, str) else ""
            segments.append({
                "index": i,
                "section": _norm_section(clip.get("section", "body")),
                "file": os.path.abspath(clip["video_path"]),
                "start_s": 0.0,
                "end_s": round(dur, 3) if dur > 0 else 1.0,
                "audio_ref": ref,
                "plan_index": clip.get("plan_index"),
            })
        segments.sort(key=lambda s: (_section_rank(s["section"]), s["index"]))
        for i, seg in enumerate(segments, start=1):
            seg["index"] = i
            if not seg["audio_ref"]:
                sid = clips[i - 1].get("slot_id")
                seg["audio_ref"] = f"v{sid}" if sid is not None and not isinstance(sid, str) else ""
        decision["segments"] = segments
        return decision

    def _build_audio_tracks(self, decision: dict, videos: list[dict], audio: list[dict]) -> dict:
        """Reconstruit les pistes audio de façon déterministe.

        - musique : première piste type=music téléchargée, toute la vidéo, vol 0.35
        - VO : UNE piste par plan (VO 'v{slot}'), volume 0.9 — un plan multi-assets
          voit sa VO couvrir la SOMME de ses segments (jamais dupliquée)
        - SFX : conservés depuis le LLM (choix créatif), chemins normalisés
        Les refs VO sont positionnés à l'ordre des voiceovers téléchargés."""
        segments = sorted(decision["segments"], key=lambda s: s["index"])
        total = sum(s["end_s"] - s["start_s"] for s in segments)

        music = [a for a in audio if a.get("type") == "music" and a.get("path")]
        audio_by_slot = self._audio_by_slot(audio)

        tracks = []
        if music:
            m = music[0]
            tracks.append({
                "ref": str(m.get("slot_id") or "a1"),
                "path": os.path.abspath(m["path"]),
                "type": "music",
                "start_s": 0.0,
                "trim_start_s": 0.0,
                "trim_dur_s": round(total, 3),
                "volume": 0.35,
            })

        vo_by_slot = {}
        for s in segments:
            ref = s["audio_ref"]
            cand = audio_by_slot.get(ref) or audio_by_slot.get(str(ref).lstrip("v"))
            vo_by_slot[str(s["index"])] = {"ref": ref, "path": cand["path"] if cand else ""}

        start_s = 0.0
        for group in self._plan_groups(segments):
            dur = group["duration"]
            first = group["segments"][0]
            ref = first["audio_ref"]
            vo = vo_by_slot.get(str(first["index"]))
            if not vo or not ref:
                start_s += dur
                continue
            cand = audio_by_slot.get(ref)
            tracks.append({
                "ref": ref,
                "path": cand["path"] if cand else vo["path"],
                "type": "voiceover",
                "start_s": round(start_s, 3),
                "trim_start_s": 0.0,
                "trim_dur_s": round(dur, 3),
                "volume": 0.9,
            })
            start_s += dur

        # SFX du LLM (créatif) — gardés, chemins déjà normalisés par _resolve_structure
        for tr in decision.get("audio_tracks", []):
            if tr.get("type") == "sfx" and os.path.exists(tr.get("path", "")):
                tracks.append({
                    "ref": tr.get("ref") or f"s{len(tracks)+1}",
                    "path": os.path.abspath(tr["path"]),
                    "type": "sfx",
                    "start_s": float(tr.get("start_s", 0) or 0),
                    "trim_start_s": float(tr.get("trim_start_s", 0) or 0),
                    "trim_dur_s": float(tr.get("trim_dur_s", 0) or 0),
                    "volume": float(tr.get("volume", 0.8)),
                })

        tracks.sort(key=lambda t: (0 if t["type"] == "music" else 1, t["start_s"]))
        decision["audio_tracks"] = tracks
        return decision

    def _build_subtitles(self, decision: dict, videos: list[dict], audio: list[dict]) -> dict:
        """Sous-titres par VO : une ligne par plan avec une VO disponible
        (Un plan multi-assets => 1 sous-titre couvrant la somme de ses segments)."""
        audio_by_slot = self._audio_by_slot(audio)
        segments = sorted(decision["segments"], key=lambda s: s["index"])
        start_s = 0.0
        subs = []
        for group in self._plan_groups(segments):
            dur = group["duration"]
            vo = audio_by_slot.get(group["segments"][0]["audio_ref"])
            if vo and vo.get("text"):
                subs.append({
                    "text": vo["text"].replace('"', "'"),
                    "start_s": round(start_s, 3),
                    "end_s": round(start_s + dur, 3),
                })
            start_s += dur
        if subs:
            decision["subtitles"] = subs
        return decision

    @staticmethod
    async def _measure_vo_durations(decision: dict) -> dict:
        """Mesure (ffprobe) la durée réelle de chaque piste voiceover et la range
        dans duration_s pour que la validation Pydantic puisse vérifier qu'une VO
        ne dépasse pas la longueur de son segment (sinon fin coupée au montage)."""
        for tr in decision.get("audio_tracks", []):
            if tr.get("type") != "voiceover" or not tr.get("path"):
                continue
            try:
                info = await ffprobe(tr["path"])
                tr["duration_s"] = round(float(info.get("duration_s", 0) or 0), 3)
            except Exception:
                tr["duration_s"] = 0.0
        return decision

    def _normalize_timestamps(self, decision: dict, videos: list[dict]) -> dict:
        """Borne chaque segment à la durée réelle de son clip (start=0)."""
        duration_by_path: dict[str, float] = {}
        for v in videos:
            d = float(v.get("duration_s", 0) or 0)
            if v.get("video_path"):
                duration_by_path[os.path.abspath(v["video_path"])] = d
        for seg in decision.get("segments", []):
            dur = duration_by_path.get(os.path.abspath(seg.get("file", "")), 0)
            if dur > 0:
                seg["start_s"] = 0.0
                seg["end_s"] = round(dur, 3)
            else:
                start = float(seg.get("start_s", 0) or 0)
                end = float(seg.get("end_s", 0) or 0)
                if end <= start:
                    end = start + 1.0
                seg["start_s"] = start
                seg["end_s"] = round(end, 3)
        return decision

    # ------------------------------------------------------------------ #
    # Cycle de vie du nœud
    # ------------------------------------------------------------------ #
    async def prep_async(self, shared):
        shared["_current_step"] = "montage_planner"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("montage_planner")
        videos = shared.get("generated_videos", [])
        audio = shared.get("downloaded_audio", [])
        retry_hint = shared.get("_llm_retry_hint", "")
        ctx = (
            f"Établis le plan de montage final en liant chaque élément du script "
            f"aux assets réellement générés.\n"
            f"Thème: {shared.get('topic', '')}\n"
            f"Script (ordre narratif à respecter HOOK -> BODY -> CTA):\n"
            f"{shared.get('script', '')[:3000]}\n\n"
            f"Clips vidéo générés (chemins et duration_s réels À UTILISER EXACTEMENT):\n"
            f"{json.dumps(videos, ensure_ascii=False)}\n\n"
            f"Pistes audio générées:\n"
            f"{json.dumps(audio, ensure_ascii=False)}\n"
        )
        if retry_hint:
            ctx += f"\n--- REPONSE PRECEDENTE INVALIDE ---\n{retry_hint}\n"
        ctx += (
            f"\nRetourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Contraintes :\n"
            f"- UN segment par clip de generated_videos, CHAQUE clip EXACTEMENT UNE FOIS, "
            f"dans l'ordre du script.\n"
            f"- Utilise les video_path et duration_s EXACTS : start_s=0, end_s=duration_s (pas 4s).\n"
            f"- audio_ref d'un segment = ref de la VO dominante (v1, vo_v1...).\n"
            f"- audio_tracks : la musique type=music couvre toute la vidéo (volume 0.35, "
            f"trim_dur_s=durée totale). Chaque VO type=voiceover est placée sur son segment "
            f"(volume 0.9). Les refs de VO commencent par 'v'.\n"
            f"- SFX type=sfx (optionnels) au moment de leur transition (volume 0.8).\n"
            f"- subtitles : une entrée par VO (texte exact de la VO, start_s=start de la VO, "
            f"end_s=fin de la VO).\n"
            f"N'utilise JAMAIS de guillemets doubles (\" ) dans les valeurs : "
            f"remplace-les par des apostrophes simples (').\n"
            f'Format : {{"title": "...", "segments": [{{"index": 1, "section": "hook", '
            f'"file": "chemin_absolu_clip", "start_s": 0, "end_s": 3.06, "audio_ref": "v1"}}], '
            f'"audio_tracks": [{{"ref": "a1", "path": "chemin_musique", "type": "music", '
            f'"start_s": 0, "trim_start_s": 0, "trim_dur_s": 18.3, "volume": 0.35}}, '
            f'{{"ref": "v1", "path": "chemin_vo_1", "type": "voiceover", "start_s": 0, '
            f'"trim_start_s": 0, "trim_dur_s": 2.5, "volume": 0.9}}], '
            f'"subtitles": [{{"text": "Phrase de la VO", "start_s": 0, "end_s": 2.5}}]}}'
        )
        resp = await call_llm(LLM_MODEL, soul, ctx)
        _trace_llm(shared, "montage_planner", "exec", LLM_MODEL, soul, ctx, resp)
        try:
            decision = _extract_json(resp)
            decision = self._resolve_structure(decision, videos, audio)
            decision = self._build_segments(decision, videos)
            decision = self._normalize_timestamps(decision, videos)
            decision = self._build_audio_tracks(decision, videos, audio)
            decision = self._build_subtitles(decision, videos, audio)
            decision = await self._measure_vo_durations(decision)
            MontageLinkPlan(**decision)
        except LLMJSONQuoteError as e:
            shared["_llm_retry_hint"] = (
                "Ta réponse contenait des guillemets doubles non échappés, "
                "le JSON était donc invalide. Réécris en remplaçant TOUS les "
                "guillemets doubles par des apostrophes simples (')."
            )
            log.warning(f"MontagePlanner retry: guillemets internes ({str(e)[:120]})")
            raise
        except ValueError as e:
            shared["_llm_retry_hint"] = f"Ta réponse n'était pas un JSON exploitable : {e}"
            log.warning(f"MontagePlanner retry: validation ({str(e)[:160]})")
            raise
        shared["_llm_retry_hint"] = ""
        n_seg = len(decision.get("segments", []))
        n_audio = len(decision.get("audio_tracks", []))
        n_subs = len(decision.get("subtitles", []))
        log.info(f"MontagePlanner -> {n_seg} segments, {n_audio} audio, {n_subs} subtitles")
        return json.dumps(decision, ensure_ascii=False)

    @staticmethod
    def _render_assets(decision: dict) -> str:
        lines = [f"## Plan de Montage — {decision.get('title', '')}"]
        lines.append("\n### Pistes Audio")
        for tr in decision.get("audio_tracks", []):
            lines.append(
                f"- {tr.get('ref', '?')} — {tr.get('type', '?')} — "
                f"{os.path.basename(tr.get('path', ''))} (vol {tr.get('volume', 0.9)})"
            )
        lines.append("\n### Séquence")
        for seg in decision.get("segments", []):
            lines.append(
                f"- {seg.get('section', 'body')} — {os.path.basename(seg.get('file', ''))} "
                f"({seg.get('start_s', 0):.2f}-{seg.get('end_s', 0):.2f}s) "
                f"audio={seg.get('audio_ref') or '-'}"
            )
        if decision.get("subtitles"):
            lines.append("\n### Sous-titres")
            for sb in decision.get("subtitles", []):
                lines.append(f"- [{sb.get('start_s', 0):.1f}-{sb.get('end_s', 0):.1f}s] {sb.get('text', '')[:70]}")
        return "\n".join(lines)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("MontagePlanner POST -> exec is not valid JSON")
            shared["_current_step"] = "montage_planner_error"
            shared["_error"] = "MontagePlanner: exec is not valid JSON"
            shared["steps"].append({
                "step": "montage_planner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("MontagePlanner aborted: exec is not valid JSON")

        segments = decision.get("segments", [])
        if not segments:
            log.error("MontagePlanner POST -> no segments")
            shared["_current_step"] = "montage_planner_error"
            shared["_error"] = "MontagePlanner: no segments"
            shared["steps"].append({
                "step": "montage_planner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("MontagePlanner aborted: no segments")

        shared["montage_structure"] = decision
        shared["assets"] = self._render_assets(decision)
        shared["_current_step"] = "montage_planner_done"
        shared["steps"].append({
            "step": "montage_planner", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("script", "")[:500],
            "output": f"{len(segments)} segments, {len(decision.get('audio_tracks', []))} audio",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"