import asyncio
import json
import logging
import os

log = logging.getLogger("pocketflow-pipeline")


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stderr.decode(errors="replace")


async def ffprobe(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: ...{stderr.decode()[-300:]}")
    data = json.loads(stdout.decode())
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    astream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    info = {
        "duration_s": float(data.get("format", {}).get("duration", 0) or 0),
        "width": int(vstream.get("width", 0)) if vstream else 0,
        "height": int(vstream.get("height", 0)) if vstream else 0,
        "vcodec": vstream.get("codec_name", "") if vstream else "",
        "acodec": astream.get("codec_name", "") if astream else "",
        "fps": 0,
    }
    if vstream and vstream.get("avg_frame_rate") and vstream["avg_frame_rate"] != "0/0":
        num, den = vstream["avg_frame_rate"].split("/")
        try:
            info["fps"] = float(num) / float(den) if float(den) else 0
        except ValueError:
            pass
    return info


async def clip_segment(input_path: str, output_path: str, start_s: float, end_s: float) -> None:
    dur = max(0.1, end_s - start_s)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-i", input_path,
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    code, err = await _run(cmd)
    if code != 0:
        raise RuntimeError(f"clip failed for {input_path}: ...{err[-500:]}")


async def concat_videos(inputs: list[str], output_path: str) -> None:
    if not inputs:
        raise RuntimeError("concat: no input clips")
    if len(inputs) == 1:
        cmd = [
            "ffmpeg", "-y",
            "-i", inputs[0],
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
    else:
        concat_list = "/tmp/opencode/ve_concat_%s.txt" % (__import__("uuid").uuid4().hex[:8])
        with open(concat_list, "w") as f:
            for p in inputs:
                f.write(f"file '{os.path.abspath(p)}'\n")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_list,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
    code, err = await _run(cmd)
    if code != 0:
        raise RuntimeError(f"concat failed: ...{err[-500:]}")


async def upscale_lanczos(video_path: str, dest_dir: str, name: str,
                           width: int = 1080, height: int = 1920) -> str:
    """Upscale une vidéo vers `width`×`height` via ffmpeg lanczos (x264)."""
    os.makedirs(dest_dir, exist_ok=True)
    output = os.path.join(dest_dir, f"{name}.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"scale={width}:{height}:flags=lanczos",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        output,
    ]
    code, err = await _run(cmd)
    if code != 0:
        raise RuntimeError(f"upscale_lanczos failed: ...{err[-500:]}")
    return output


async def mix_audio(video_path: str, audio_tracks: list[dict], output_path: str, video_duration_s: float) -> None:
    """audio_tracks: [{path, start_s, trim_start_s, trim_dur_s, volume}] — mixe les pistes
    sous la vidéo. start_s = offset dans la timeline finale (adelay), volume = gain."""
    if not audio_tracks:
        return await concat_videos([video_path], output_path)

    cmd = ["ffmpeg", "-y", "-i", video_path]
    for tr in audio_tracks:
        cmd += ["-ss", f"{tr.get('trim_start_s', 0):.3f}", "-t", f"{tr.get('trim_dur_s', video_duration_s):.3f}", "-i", tr["path"]]

    inputs_n = len(audio_tracks)
    filter_parts = []
    for i in range(inputs_n):
        vol = float(audio_tracks[i].get("volume", 0.9))
        start_ms = int(float(audio_tracks[i].get("start_s", 0)) * 1000)
        chain = f"[{i+1}:a]aresample=48000,volume={vol:.2f}"
        if start_ms > 0:
            chain += f",adelay={start_ms}|{start_ms}"
        chain += f"[a{i}]"
        filter_parts.append(chain)
    mix_expr = "".join(f"[a{i}]" for i in range(inputs_n))
    if inputs_n > 1:
        filter_parts.append(f"{mix_expr}amix=inputs={inputs_n}:normalize=0[mix]")
    else:
        filter_parts.append("[a0]anull[mix]")
    # apad : la piste la plus courte (souvent la musique, ou une VO générée plus
    # courte que prévu) est complétée par du silence jusqu'à la durée de la
    # vidéo. Sans ça, `-shortest` plus bas tronque la VIDÉO à la durée de
    # l'audio le plus court — perte de fin de vidéo constatée (22s de musique
    # pour 47s de vidéo).
    filter_parts.append(f"[mix]apad[mixpad]")
    filter_parts.append("[0:v]fps=30,format=yuv420p[v]")
    filter_parts.append(f"[mixpad]atrim=0:{video_duration_s:.3f},asetpts=PTS-STARTPTS[aout]")
    filter_complex = ";".join(filter_parts)

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_path,
    ]
    code, err = await _run(cmd)
    if code != 0:
        raise RuntimeError(f"mix_audio failed: ...{err[-500:]}")


def _srt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(subtitles: list[dict], srt_path: str) -> str:
    """subtitles: [{text, start_s, end_s}] -> écrit un .srt (contenu en dur dans les valeurs)."""
    lines = []
    for i, sub in enumerate(subtitles, start=1):
        start = max(0.0, float(sub.get("start_s", 0)))
        end = start + max(0.2, float(sub.get("end_s", start + 1.0)) - start)
        text = sub.get("text", "").strip().replace("\n", " ")
        if not text:
            continue
        lines.append(f"{i}")
        lines.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
        lines.append(text)
        lines.append("")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return srt_path


def write_ass(subtitles: list[dict], ass_path: str,
              width: int = 704, height: int = 1280) -> str:
    """subtitles: [{text, start_s, end_s}] -> écrit un .ass style Luckiest Guy punchy.

    Police Luckiest Guy, contour noir massif, texte blanc, pop zoom à l'entrée."""
    def _ass_ts(s: float) -> str:
        h, rem = divmod(max(0.0, s), 3600)
        m, s2 = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{s2:05.2f}"

    # Échelle depuis le template 1080x1920
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
        "&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,2,0,1,{outline},{shadow},5,40,40,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for sub in subtitles:
        start = max(0.0, float(sub.get("start_s", 0)))
        end = start + max(0.3, float(sub.get("end_s", start + 1.0)) - start)
        text = sub.get("text", "").strip().replace("\n", " ")
        if not text:
            continue

        colored = (
            f"{{\\pos({pos_x},{pos_y})\\an5"
            f"\\fscx115\\fscy115\\t(0,140,\\fscx100\\fscy100)"
            f"}}{text}"
        )

        events.append(
            f"Dialogue: 0,{_ass_ts(start)},{_ass_ts(end)},LuckiestPunch,,0,0,0,,{colored}"
        )
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")
    return ass_path


async def burn_subtitles(video_path: str, ass_path: str, output_path: str,
                         fontsdir: str | None = None) -> None:
    """Brûle le .ass dans la vidéo (sous-titres incrustés via libass)."""
    if not os.path.exists(ass_path):
        raise RuntimeError(f"burn_subtitles: ass missing: {ass_path}")
    escaped = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    vf = f"ass={escaped}"
    if fontsdir:
        fonts_esc = fontsdir.replace("\\", "/").replace(":", "\\:")
        vf += f":fontsdir={fonts_esc}"
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "copy",
        output_path,
    ]
    code, err = await _run(cmd)
    if code != 0:
        raise RuntimeError(f"burn_subtitles failed: ...{err[-500:]}")
