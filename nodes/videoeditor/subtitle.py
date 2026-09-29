import json
import logging
import os
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import SUBTITLE_FONTS_DIR
from helpers.ffmpeg import ffprobe, write_ass, burn_subtitles
from helpers.whisper_words import transcribe_words
from helpers.state import _set_state, _shared_snapshot, _set_traces

log = logging.getLogger("pocketflow-pipeline")

MAX_WORDS_PER_LINE = 8
PAUSE_THRESHOLD_S = 0.35


def _group_words_into_lines(words: list[dict]) -> list[dict]:
    """Groupe les mots en lignes de sous-titres.

    Découpe sur les pauses (> PAUSE_THRESHOLD_S) ou quand MAX_WORDS_PER_LINE
    est atteint. Retourne [{text, start_s, end_s}].
    """
    if not words:
        return []
    lines = []
    buf = [words[0]]
    for w in words[1:]:
        prev = buf[-1]
        gap = w["start_s"] - prev["end_s"]
        if gap > PAUSE_THRESHOLD_S or len(buf) >= MAX_WORDS_PER_LINE:
            lines.append({
                "text": " ".join(bw["word"].strip() for bw in buf),
                "start_s": buf[0]["start_s"],
                "end_s": buf[-1]["end_s"],
            })
            buf = [w]
        else:
            buf.append(w)
    if buf:
        lines.append({
            "text": " ".join(bw["word"].strip() for bw in buf),
            "start_s": buf[0]["start_s"],
            "end_s": buf[-1]["end_s"],
        })
    return lines


def _write_ass_wordsync(words: list[dict], ass_path: str,
                        width: int = 704, height: int = 1280) -> str:
    """Écrit un .ass avec highlight karaoke mot-par-mot.

    Une seule Dialogue line par phrase. Le tag \\kf déclenche le fill jaune
    mot par mot sans ré-animer le pop zoom (qui ne se joue qu'une fois).
    PrimaryColour = jaune (fill), SecondaryColour = blanc (idle).
    """
    def _ass_ts(s: float) -> str:
        h, rem = divmod(max(0.0, s), 3600)
        m, s2 = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{s2:05.2f}"

    sc = min(width / 1080.0, height / 1920.0)
    fontsize = max(20, int(105 * sc))
    outline = max(2, int(9 * sc))
    shadow = max(1, int(4 * sc))
    pos_x = int(540 * sc)
    pos_y = int(1450 * sc)

    header = (
        "[Script Info]\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScriptType: v4.00+\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: LuckiestPunch,Luckiest Guy,{fontsize},"
        "&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,2,0,1,{outline},{shadow},5,40,40,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = _group_words_into_lines(words)
    events = []
    for line in lines:
        line_words = [w for w in words
                      if w["end_s"] > line["start_s"] and w["start_s"] < line["end_s"]]
        if not line_words:
            continue

        kf_parts = []
        for w in line_words:
            dur_cs = max(1, int(round((w["end_s"] - w["start_s"]) * 100)))
            kf_parts.append(f"{{\\kf{dur_cs}}}{w['word'].strip()}")
        text = " ".join(kf_parts)

        colored = (
            f"{{\\pos({pos_x},{pos_y})\\an5"
            f"\\fscx115\\fscy115\\t(0,140,\\fscx100\\fscy100)"
            f"}}{text}"
        )
        events.append(
            f"Dialogue: 0,{_ass_ts(line['start_s'])},{_ass_ts(line['end_s'])},"
            f"LuckiestPunch,,0,0,0,,{colored}"
        )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")
    return ass_path


class SubtitleNode(AsyncNode):
    """Génère et brûle les sous-titres dans la vidéo finale.

    Priorité :
    1. Whisper transcription → karaoke mot-par-mot
    2. Fallback : sous-titres LLM du planner (phrase-level)
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "subtitle"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        final_path = shared.get("ve_final_path")
        if not final_path or not os.path.exists(final_path):
            raise RuntimeError(f"SubtitleNode: final video missing: {final_path}")

        info = await ffprobe(final_path)
        width = info.get("width", 704)
        height = info.get("height", 1280)
        project_dir = shared.get("ve_project_dir", "")

        vo_audio = [a for a in shared.get("downloaded_audio", [])
                    if a.get("type") == "voiceover" and a.get("path")]

        audio_tracks = shared.get("montage_structure", {}).get("audio_tracks", [])
        vo_tracks = [tr for tr in audio_tracks if tr.get("ref", "").startswith("v")]

        words_all = []
        if vo_audio:
            for i, vo in enumerate(vo_audio):
                audio_path = vo["path"]
                if not os.path.isfile(audio_path):
                    log.warning(f"SubtitleNode: VO audio missing: {audio_path}")
                    continue
                words = await transcribe_words(audio_path)
                if words:
                    offset = float(vo_tracks[i].get("start_s", 0)) if i < len(vo_tracks) else 0.0
                    for w in words:
                        w["start_s"] += offset
                        w["end_s"] += offset
                    words_all.extend(words)
                    log.info(f"SubtitleNode: whisper {os.path.basename(audio_path)} -> {len(words)} words (offset={offset:.2f}s)")

        ass_path = os.path.join(project_dir, "subtitles.ass")
        subbed_out = os.path.join(project_dir, "final_subbed.mp4")

        if words_all:
            words_all.sort(key=lambda w: w["start_s"])
            _write_ass_wordsync(words_all, ass_path, width, height)
            log.info(f"SubtitleNode: word-sync ASS -> {len(words_all)} words, {len(_group_words_into_lines(words_all))} lines")
        else:
            log.info("SubtitleNode: whisper unavailable, falling back to planner subtitles")
            subs = shared.get("montage_structure", {}).get("subtitles", [])
            if not subs:
                log.info("SubtitleNode: no subtitles at all, skipping")
                return {"skipped": True}
            write_ass(subs, ass_path, width, height)

        await burn_subtitles(final_path, ass_path, subbed_out, fontsdir=SUBTITLE_FONTS_DIR)
        log.info(f"SubtitleNode: burned subtitles -> {subbed_out}")

        return {
            "final_path": subbed_out,
            "ass_path": ass_path,
            "n_words": len(words_all),
            "n_lines": len(_group_words_into_lines(words_all)) if words_all else 0,
        }

    async def post_async(self, shared, prep, exec):
        if exec.get("skipped"):
            shared["_current_step"] = "subtitle_done"
            shared["steps"].append({
                "step": "subtitle", "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("ve_final_path", ""),
                "output": "skipped (no subtitles)",
            })
            await _set_state(**_shared_snapshot(shared))
            await _set_traces(shared.get("_traces", {}))
            return "default"

        shared["ve_final_path"] = exec["final_path"]
        shared["_current_step"] = "subtitle_done"
        shared["steps"].append({
            "step": "subtitle", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("ve_final_path", ""),
            "output": f"{exec['n_words']} words, {exec['n_lines']} lines",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
