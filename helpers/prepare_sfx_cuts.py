#!/usr/bin/env python3
"""Découpe les packs SFX téléchargés (VFX/) en effets unitaires.

- Détection des silences au seuil calibré par fichier.
- Découpes = plages non-silencieuses, silences courts (< min_sep) considérés
  comme pauses internes d'un même effet, micro-coupes (< min_content) écartées.
- Sortie : WAV PCM 44.1 kHz stéréo dans VFX/sfx_cut/<categorie>/<slug>_NNN.wav
  avec fades 20 ms anti-clic, niveau source conservé.
- Manifeste : VFX/sfx_library.json (préparatoire pour l'attribution dynamique).

Usage:
    python scripts/prepare_sfx_cuts.py [--ship] /media/marcs/Linux_Apps/Projets_AI/pocketflow_pipeline/VFX
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SRC_CATEGORY = {
    "Whoosh": "whooshes",
    "Pops & clicks": "pops",
    "Impacts": "impacts",
    "RIsers, Sweeps": "risers",
}

# Paramètres calibrés (analyse d'écoute préalable)
SOURCES = [
    {
        "folder": "Whoosh",
        "file": "Dramatic whoosh sound effects no copyright  whoosh sound effects for edits  whoosh sfx pack.mp3",
        "prefix": "whooshes",
        "noise_db": -35.0,
        "min_sep": 0.30,
        "min_content": 0.25,
    },
    {
        "folder": "Pops & clicks",
        "file": "Cute Pop Sound Effects.mp3",
        "prefix": "pops",
        "noise_db": -48.0,
        "min_sep": 0.30,
        "min_content": 0.15,
    },
    {
        "folder": "Impacts",
        "file": "Metal Hit - Free Sound Effect.mp3",
        "prefix": "metal_hit",
        "noise_db": -35.0,
        "min_sep": 0.30,
        "min_content": 0.25,
    },
    {
        "folder": "RIsers, Sweeps",
        "file": "Riser - Sound Effect (Free).mp3",
        "prefix": "riser",
        "noise_db": -45.0,
        "min_sep": 0.30,
        "min_content": 0.25,
    },
]

FADE = 0.02


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(r.stdout.strip())


def detect_silences(path, noise_db, min_dur):
    r = run(["ffmpeg", "-hide_banner", "-i", str(path),
             "-af", f"silencedetect=noise={noise_db:.0f}dB:d={min_dur}",
             "-f", "null", "-"])
    events = []
    pending = None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m:
            pending = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m and pending is not None:
            events.append((pending, float(m.group(1))))
            pending = None
    if pending is not None:
        events.append((pending, probe_duration(path)))
    return events


def content_intervals(duration, silences, min_content):
    intervals = []
    prev = 0.0
    for s, e in silences:
        if s - prev >= min_content:
            intervals.append([prev, s])
        prev = max(prev, e)
    if duration - prev >= min_content:
        intervals.append([prev, duration])
    return intervals


def merge_intervals(intervals, min_sep):
    merged = []
    for iv in intervals:
        if merged and iv[0] - merged[-1][1] < min_sep:
            merged[-1][1] = iv[1]
        else:
            merged.append(list(iv))
    return merged


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def cut_wav(src, start, end, out):
    dur = end - start
    filters = "atrim=0,asetpts=PTS-STARTPTS"
    if dur > FADE * 2:
        filters += f",afade=t=in:d={FADE},afade=t=out:st={max(dur - FADE, 0):.3f}:d={FADE}"
    r = run(["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
             "-ac", "2", "-ar", "44100",
             "-af", filters,
             "-y", str(out)])
    return r.returncode


def volume_stats(path):
    r = run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
             "-f", "null", "-"])
    mean = maxv = None
    for line in r.stderr.splitlines():
        m = re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", line)
        if m:
            mean = float(m.group(1))
        m = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", line)
        if m:
            maxv = float(m.group(1))
    return mean, maxv


def refresh_manifest(root: Path) -> list:
    """Re-scanne les wav existants dans sfx_cut/ et reconstruit le manifeste.

    Conserve la traçabilité (source, bornes) des entrées déjà connues."""
    lib_path = root / "sfx_library.json"
    old = {}
    if lib_path.exists():
        for e in json.loads(lib_path.read_text()):
            old[e["file"]] = e

    manifest = []
    for wav in sorted((root / "sfx_cut").rglob("*.wav")):
        rel = f"sfx_cut/{wav.parent.name}/{wav.name}"
        prev = old.get(rel, {})
        dfile = probe_duration(wav)
        mean, maxv = volume_stats(wav)
        manifest.append({
            "category": prev.get("category", wav.parent.name),
            "source": prev.get("source", ""),
            "file": rel,
            "start_s": prev.get("start_s"),
            "end_s": prev.get("end_s"),
            "duration_s": round(dfile, 3),
            "mean_db": round(mean, 1) if mean is not None else None,
            "max_db": round(maxv, 1) if maxv is not None else None,
        })
        print(f"   {rel:<44} ({dfile:5.2f}s) mean {mean if mean is not None else '?':>6} dB")

    lib_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="VFX", type=Path,
                    help="racine contenant les dossiers SFX")
    ap.add_argument("--refresh", action="store_true",
                    help="re-scanner les wav existants sans re-découper")
    args = ap.parse_args()

    root: Path = args.root
    if args.refresh:
        manifest = refresh_manifest(root)
        print(f"\nManifeste: {root/'sfx_library.json'} ({len(manifest)} effets)")
        return

    out_root = root / "sfx_cut"
    manifest = []
    problems = []

    for src in SOURCES:
        src_path = root / src["folder"] / src["file"]
        if not src_path.exists():
            print(f"SKIP  {src_path} : fichier absent")
            continue
        cat = SRC_CATEGORY[src["folder"]]
        prefix = src["prefix"]
        cat_dir = out_root / cat
        cat_dir.mkdir(parents=True, exist_ok=True)

        dur_all = probe_duration(src_path)
        sil = detect_silences(src_path, src["noise_db"], 0.25 if cat == "whooshes" else 0.15)
        ivs = content_intervals(dur_all, sil, src["min_content"])
        cuts = merge_intervals(ivs, src["min_sep"])

        print(f"\n── {src['folder']}/{src['file']}")
        print(f"   durée {dur_all:.2f}s | silences détectés: {len(sil)} | coupes: {len(cuts)}")

        for i, (start, end) in enumerate(cuts, 1):
            out = cat_dir / f"{prefix}_{i:03d}.wav"
            rc = cut_wav(src_path, start, end, out)
            if rc != 0:
                problems.append(f"{out.name}: ffmpeg exit {rc}")
                continue
            dfile = probe_duration(out)
            mean, maxv = volume_stats(out)
            if mean is not None and mean < -45.0:
                out.unlink()
                print(f"   [{i:02d}] {out.name:<28} DROP (quasi-silence, mean {mean:.1f}dB)")
                continue
            if dfile < 0.15:
                problems.append(f"{out.name}: durée {dfile:.2f}s trop courte")
            manifest.append({
                "category": cat,
                "source": f"{src['folder']}/{src['file']}",
                "file": f"sfx_cut/{cat}/{out.name}",
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "duration_s": round(end - start, 3),
                "mean_db": round(mean, 1) if mean is not None else None,
                "max_db": round(maxv, 1) if maxv is not None else None,
            })
            print(f"   [{i:02d}] {out.name:<28} {start:6.2f}->{end:6.2f}s "
                  f"({end - start:5.2f}s) mean {mean if mean is not None else '?':>7}dB "
                  f"max {maxv if maxv is not None else '?':>6}dB")

    lib_path = root / "sfx_library.json"
    lib_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"\nManifeste: {lib_path} ({len(manifest)} effets)")
    if problems:
        print("\n⚠ Pbmèmes:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("OK : aucune coupe suspecte")


if __name__ == "__main__":
    main()