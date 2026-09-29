"""Test de la reconstruction déterministe du plan de montage (MontagePlannerNode).

Reproduit le scénario qui plantait avec l'ancien MontagePlanner+Critic :
- clip_5 dupliqué, clip_2 oublié par le LLM
- chemins hallucinés (clip_1_raw.webm au lieu de clip_1_raw_i2v.webm)
- musique référencée sans extension, durées arrondies à 4s

Vérifie que la reconstruction déterministe garantit :
- chaque clip utilisé EXACTEMENT une fois, dans l'ordre hook -> body -> cta
- durées réelles (duration_s), pas d'arrondi
- musique vol 0.35 couvrant toute la vidéo
- VO positionnées aux offsets cumulés, refs ordonnées v1..v5
- sous-titres générés depuis les textes des VO
Aucun LLM, aucun GPU — pure logique programmatique.
"""

import collections
import contextlib
import io
import logging
import os
import sys
import tempfile
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(PIPELINE_ROOT / "nodes"))

from nodes.assetfinder.montage_planner import MontagePlannerNode, MontageLinkPlan, VO_DURATION_TOL


def _mk(tmp, name, dur):
    p = os.path.join(tmp, name)
    open(p, "wb").write(b"x")
    return p


def _capture_logs_warnings(fn):
    logger = logging.getLogger("pocketflow-pipeline")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        fn()
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def test_montage_reconstruction_deterministe():
    tmp = tempfile.mkdtemp()
    paths = {
        1: _mk(tmp, "clip_1_raw_i2v.webm", 3.06),
        2: _mk(tmp, "clip_2_raw_i2v.webm", 3.06),
        3: _mk(tmp, "clip_3_raw.webm", 4.06),
        4: _mk(tmp, "clip_4_raw.webm", 4.06),
        5: _mk(tmp, "clip_5_raw.webm", 4.06),
    }
    videos = [
        {"slot_id": 1, "section": "hook", "position": 0, "video_path": paths[1], "duration_s": 3.06},
        {"slot_id": 2, "section": "body", "position": 1, "video_path": paths[2], "duration_s": 3.06},
        {"slot_id": 3, "section": "body", "position": 2, "video_path": paths[3], "duration_s": 4.06},
        {"slot_id": 4, "section": "body", "position": 3, "video_path": paths[4], "duration_s": 4.06},
        {"slot_id": 5, "section": "cta", "position": 4, "video_path": paths[5], "duration_s": 4.06},
    ]
    audio = [
        {"slot_id": "a1", "type": "music", "path": _mk(tmp, "music_a1.mp3", 0)},
        {"slot_id": "v1", "type": "voiceover", "path": _mk(tmp, "vo_v1.wav", 0), "text": "Intro Uta"},
        {"slot_id": "v2", "type": "voiceover", "path": _mk(tmp, "vo_v2.wav", 0), "text": "Pirouette"},
        {"slot_id": "v3", "type": "voiceover", "path": _mk(tmp, "vo_v3.wav", 0), "text": "Setup gaming"},
        {"slot_id": "v4", "type": "voiceover", "path": _mk(tmp, "vo_v4.wav", 0), "text": "Cartes rewards"},
        {"slot_id": "v5", "type": "voiceover", "path": _mk(tmp, "vo_v5.wav", 0), "text": "Follow"},
    ]

    decision = {
        "title": "Test",
        "segments": [
            {"index": 1, "section": "hook", "file": os.path.join(tmp, "clip_3_raw.webm"), "start_s": 0, "end_s": 4, "audio_ref": "v1"},
            {"index": 2, "section": "body", "file": os.path.join(tmp, "clip_4_raw.webm"), "start_s": 0, "end_s": 4, "audio_ref": "v2"},
            {"index": 3, "section": "body", "file": os.path.join(tmp, "clip_5_raw.webm"), "start_s": 0, "end_s": 4, "audio_ref": "v3"},
            {"index": 4, "section": "body", "file": os.path.join(tmp, "clip_1_raw.webm"), "start_s": 0, "end_s": 4, "audio_ref": "v4"},
            {"index": 5, "section": "cta", "file": os.path.join(tmp, "clip_5_raw.webm"), "start_s": 0, "end_s": 4, "audio_ref": "v5"},
        ],
        "audio_tracks": [
            {"ref": "a1", "path": os.path.join(tmp, "music_a1"), "type": "music", "start_s": 0, "trim_start_s": 0, "trim_dur_s": 0, "volume": 1.0},
            {"ref": "v5", "path": os.path.join(tmp, "vo_v5.wav"), "type": "voiceover", "start_s": 0, "trim_start_s": 0, "trim_dur_s": 2.5, "volume": 0.9},
            {"ref": "v4", "path": os.path.join(tmp, "vo_v4.wav"), "type": "voiceover", "start_s": 0, "trim_start_s": 0, "trim_dur_s": 2.5, "volume": 0.9},
            {"ref": "v3", "path": os.path.join(tmp, "vo_v3.wav"), "type": "voiceover", "start_s": 0, "trim_start_s": 0, "trim_dur_s": 2.5, "volume": 0.9},
            {"ref": "v2", "path": os.path.join(tmp, "vo_v2.wav"), "type": "voiceover", "start_s": 0, "trim_start_s": 0, "trim_dur_s": 2.5, "volume": 0.9},
            {"ref": "v1", "path": os.path.join(tmp, "vo_v1.wav"), "type": "voiceover", "start_s": 0, "trim_start_s": 0, "trim_dur_s": 2.5, "volume": 0.9},
        ],
        "subtitles": [],
    }

    mp = MontagePlannerNode()
    d = mp._resolve_structure(decision, videos, audio)
    d = mp._build_segments(d, videos)
    d = mp._normalize_timestamps(d, videos)
    d = mp._build_audio_tracks(d, videos, audio)
    d = mp._build_subtitles(d, videos, audio)
    MontageLinkPlan(**d)

    segments = d["segments"]

    # 1) Couverture exacte, aucun doublon
    files = [os.path.basename(s["file"]) for s in segments]
    dupes = [p for p, c in collections.Counter(files).items() if c > 1]
    assert not dupes, f"clips dupliqués: {dupes}"
    assert set(files) == {
        "clip_1_raw_i2v.webm", "clip_2_raw_i2v.webm", "clip_3_raw.webm",
        "clip_4_raw.webm", "clip_5_raw.webm",
    }, files

    # 2) Ordre du script + sections
    assert [s["section"] for s in segments] == ["hook", "body", "body", "body", "cta"]
    assert [os.path.basename(s["file"]) for s in segments] == [
        "clip_1_raw_i2v.webm", "clip_2_raw_i2v.webm", "clip_3_raw.webm",
        "clip_4_raw.webm", "clip_5_raw.webm",
    ]

    # 3) Durées réelles, pas d'arrondi à 4s
    assert [round(s["end_s"], 2) for s in segments] == [3.06, 3.06, 4.06, 4.06, 4.06]
    total = sum(s["end_s"] - s["start_s"] for s in segments)
    assert abs(total - 18.31) < 0.2, total

    # 4) Musique sur toute la vidéo, vol 0.35
    music = [t for t in d["audio_tracks"] if t["type"] == "music"][0]
    assert music["volume"] == 0.35
    assert abs(music["trim_dur_s"] - total) < 0.2

    # 5) VO : refs ordonnées et positions cumulées
    vo_tracks = [t for t in d["audio_tracks"] if t["type"] == "voiceover"]
    assert [t["ref"] for t in vo_tracks] == ["v1", "v2", "v3", "v4", "v5"]
    starts = [t["start_s"] for t in vo_tracks]
    assert abs(starts[0] - 0.0) < 0.01
    assert abs(starts[1] - 3.06) < 0.01
    assert abs(starts[2] - 6.12) < 0.01
    assert abs(starts[3] - 10.18) < 0.01
    assert abs(starts[4] - 14.24) < 0.01

    # 6) Sous-titres générés depuis les textes des VO
    assert len(d["subtitles"]) == 5
    assert d["subtitles"][0]["text"] == "Intro Uta"

    # 7) L'assets markdown est lisible
    assets = mp._render_assets(d)
    assert "## Plan de Montage" in assets
    assert "clip_1_raw_i2v.webm" in assets

    print("test_montage_reconstruction_deterministe PASSED")


def test_montage_vo_trop_longue_avertit(caplog=None):
    """"_measure_vo_durations récupère la durée réelle d'une VO, puis le
    model_validator alerte au log (sans échouer) si la VO dépasse son segment."""
    tmp = tempfile.mkdtemp()
    clip = _mk(tmp, "clip_1_raw.webm", 0)
    vo = _mk(tmp, "vo_v1.wav", 0)
    decision = {
        "title": "Test",
        "segments": [
            {"index": 1, "section": "hook", "file": clip, "start_s": 0, "end_s": 3.06, "audio_ref": "v1"},
        ],
        "audio_tracks": [
            {"ref": "v1", "path": vo, "type": "voiceover", "start_s": 0, "trim_start_s": 0,
             "trim_dur_s": 3.06, "volume": 0.9, "duration_s": 5.4},
        ],
        "subtitles": [],
    }

    if caplog is not None:
        with caplog.at_level("WARNING", logger="pocketflow-pipeline"):
            MontageLinkPlan(**decision)
        lines = "".join(r.message + "\n" for r in caplog.records)
    else:
        lines = _capture_logs_warnings(lambda: MontageLinkPlan(**decision))

    assert "vo_v1.wav" in lines and "coupée" in lines
    print("test_montage_vo_trop_longue_avertit PASSED")


def test_montage_vo_courte_passe_sans_avertissement(caplog=None):
    tmp = tempfile.mkdtemp()
    clip = _mk(tmp, "clip_1_raw.webm", 0)
    vo = _mk(tmp, "vo_v1.wav", 0)
    decision = {
        "title": "Test",
        "segments": [
            {"index": 1, "section": "hook", "file": clip, "start_s": 0, "end_s": 3.06, "audio_ref": "v1"},
        ],
        "audio_tracks": [
            {"ref": "v1", "path": vo, "type": "voiceover", "start_s": 0, "trim_start_s": 0,
             "trim_dur_s": 3.06, "volume": 0.9, "duration_s": 2.8},
        ],
        "subtitles": [],
    }

    if caplog is not None:
        with caplog.at_level("WARNING", logger="pocketflow-pipeline"):
            MontageLinkPlan(**decision)
        lines = "".join(r.message + "\n" for r in caplog.records)
    else:
        lines = _capture_logs_warnings(lambda: MontageLinkPlan(**decision))

    assert not ("vo_v1.wav" in lines and "coupée" in lines)
    assert VO_DURATION_TOL == 0.2
    print("test_montage_vo_courte_passe_sans_avertissement PASSED")


def test_montage_plan_multi_assets_vo_unique():
    """Plan multi-assets (ex: I2V 3.06s + T2V 4.06s = 7.12s) : la VO UNIQUE du
    plan doit être placée UNE SEULE fois et couvrir la SOMME des segments —
    jamais dupliquée par segment. (Modèle 2-assets validé avec l'user.)"""
    tmp = tempfile.mkdtemp()
    paths = {
        1: _mk(tmp, "clip_1_raw_i2v.webm", 3.06),
        2: _mk(tmp, "clip_2_raw.webm", 4.06),
        3: _mk(tmp, "clip_3_raw.webm", 4.06),
    }
    videos = [
        {"slot_id": 1, "section": "hook", "position": 0, "plan_index": 1, "video_path": paths[1], "duration_s": 3.06},
        {"slot_id": 2, "section": "hook", "position": 1, "plan_index": 1, "video_path": paths[2], "duration_s": 4.06},
        {"slot_id": 3, "section": "body", "position": 2, "plan_index": 2, "video_path": paths[3], "duration_s": 4.06},
    ]
    audio = [
        {"slot_id": "a1", "type": "music", "path": _mk(tmp, "music_a1.mp3", 0)},
        {"slot_id": "v1", "type": "voiceover", "path": _mk(tmp, "vo_v1.wav", 0), "text": "If your favorite is Ruan Mei I have bad news"},
        {"slot_id": "v2", "type": "voiceover", "path": _mk(tmp, "vo_v2.wav", 0), "text": "And now the follow"},
    ]
    decision = {
        "title": "Test multi-assets",
        "segments": [
            {"index": 1, "section": "hook", "file": os.path.join(tmp, "clip_1_raw_i2v.webm"), "start_s": 0, "end_s": 3.06, "audio_ref": "v1"},
            {"index": 2, "section": "hook", "file": os.path.join(tmp, "clip_2_raw.webm"), "start_s": 0, "end_s": 4.06, "audio_ref": "v1"},
            {"index": 3, "section": "body", "file": os.path.join(tmp, "clip_3_raw.webm"), "start_s": 0, "end_s": 4.06, "audio_ref": "v2"},
        ],
        "audio_tracks": [],
        "subtitles": [],
    }

    mp = MontagePlannerNode()
    d = mp._resolve_structure(decision, videos, audio)
    d = mp._build_segments(d, videos)
    d = mp._normalize_timestamps(d, videos)
    d = mp._build_audio_tracks(d, videos, audio)
    d = mp._build_subtitles(d, videos, audio)
    MontageLinkPlan(**d)

    # segments : plan_index reporté pour grouper
    plans = [s["plan_index"] for s in d["segments"]]
    assert plans == [1, 1, 2], plans

    # UNE seule VO pour le plan 1, couvrant la somme (7.12s)
    vo = [t for t in d["audio_tracks"] if t["type"] == "voiceover"]
    assert [t["ref"] for t in vo] == ["v1", "v2"]
    v1 = vo[0]
    assert v1["start_s"] == 0.0
    assert abs(v1["trim_dur_s"] - 7.12) < 0.01, v1["trim_dur_s"]
    assert abs(vo[1]["start_s"] - 7.12) < 0.01
    assert abs(vo[1]["trim_dur_s"] - 4.06) < 0.01

    # sous-titres : un seul par plan, plan 1 couvrant la somme
    assert [s["text"] for s in d["subtitles"]] == [
        "If your favorite is Ruan Mei I have bad news",
        "And now the follow",
    ]
    assert abs(d["subtitles"][0]["end_s"] - 7.12) < 0.01
    assert abs(d["subtitles"][1]["start_s"] - 7.12) < 0.01

    # musique couvre le total
    music = [t for t in d["audio_tracks"] if t["type"] == "music"][0]
    assert abs(music["trim_dur_s"] - 11.18) < 0.2

    print("test_montage_plan_multi_assets_vo_unique PASSED")


if __name__ == "__main__":
    test_montage_reconstruction_deterministe()
    test_montage_vo_trop_longue_avertit()
    test_montage_vo_courte_passe_sans_avertissement()
    test_montage_plan_multi_assets_vo_unique()