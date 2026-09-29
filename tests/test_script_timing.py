"""Test du moteur de timing/pacing des scripts SW (nodes/scriptwriter/script_timing.py).

Modèle validé avec l'user :
- UN beat = UN plan ; une VO trop longue N'EST JAMAIS coupée ni splittée.
- MAX 2 vidéos par plan (I2V≈3s + T2V≈4s = 7s ≈ 18 mots, 2 T2V = 8s ≈ 20 mots) :
  une VO trop longue se RACCOURCIT, elle n'ajoute jamais de 3e vidéo.
- La durée d'un plan = SOMME des durées de ses assets (I2V≈3s, T2V≈4s).
- Seulement 2 I2V par script : 1re Video: du Plan 1 + 1re Video: du Plan 2.
Aucun LLM, aucun GPU — pure logique déterministe.
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from nodes.scriptwriter.script_timing import (
    I2V_SEC,
    T2V_SEC,
    asset_duration_secs,
    asset_kind,
    pacing_errors,
    parse_plans,
    plan_budget_words,
    plan_required_duration,
    repair_pacing_script,
    rewrite_plan_timestamps,
)

from nodes.scriptwriter.pydantic_validation import GeneratedScriptAlt

SCRIPT_7S = """# HOOK (0-7s)
- Plan 1 (0-7s)
- Video: Saber Alter threatening the screen, cinematic
- Video: Alternate camera angle, epic pan up her silhouette
- VO: If your favorite is Ruan Mei, I have bad news.
- Plan 2 (7-10s)
- Video: The black-armored swordswoman advancing
- VO: The banner just dropped.
"""


def test_regle_i2v_2_assets_seulement():
    # 1re Video des Plans 1 et 2 = I2V, tout le reste T2V
    assert asset_kind(1, 0) == "i2v"
    assert asset_kind(2, 0) == "i2v"
    assert asset_kind(1, 1) == "t2v"
    assert asset_kind(2, 1) == "t2v"
    assert asset_kind(3, 0) == "t2v"
    assert asset_kind(4, 0) == "t2v"
    assert asset_duration_secs(1, 0) == I2V_SEC
    assert asset_duration_secs(1, 1) == T2V_SEC


def test_plan_requis_7s_pour_2_assets():
    plans = parse_plans(SCRIPT_7S)
    p1 = plans[0]
    assert len(p1["video_lines"]) == 2
    assert plan_required_duration(p1) == I2V_SEC + T2V_SEC  # 3 + 4 = 7s
    p2 = plans[1]
    assert len(p2["video_lines"]) == 1
    # 1re Video du Plan 2 = 2e (et dernier) I2V : 3s
    assert plan_required_duration(p2) == I2V_SEC


def test_pacing_ok_7s():
    # VO 10 mots dans un plan de 7s (budget 2*7+4=18) -> pas d'erreur
    assert pacing_errors(SCRIPT_7S) == []


def test_vo_trop_longue_detectee():
    script = """- Plan 1 (0-3s)
- Video: The black-armored swordswoman advancing
- VO: If your favorite is Ruan Mei I have bad news indeed.
"""
    errs = pacing_errors(script)
    assert any("VO trop longue" in e for e in errs), errs


def test_horaires_incoherents_detectes():
    script = """- Plan 1 (0-3s)
- Video: The black-armored swordswoman advancing
- Video: Alternate camera angle, epic pan
- VO: Short line here.
"""
    errs = pacing_errors(script)
    assert any("somme de ses assets" in e for e in errs), errs
    assert not any("VO trop longue" in e for e in errs), errs


def test_budget_parole_grandit_avec_assets():
    p1bar = {"num": 1, "video_lines": [{"idx": 0}]}
    p2a = {"num": 1, "video_lines": [{"idx": 0}, {"idx": 1}]}
    assert plan_budget_words(p1bar) < plan_budget_words(p2a)


def test_budget_parole_sature_a_2_assets():
    # la 3e vidéo ne doit RIEN ajouter au budget parole : max 2 par plan
    p2a = {"num": 1, "video_lines": [{"idx": 0}, {"idx": 1}]}
    p3 = {"num": 1, "video_lines": [{"idx": 0}, {"idx": 1}, {"idx": 2}]}
    assert plan_budget_words(p3) == plan_budget_words(p2a)


def test_plan_plus_de_2_videos_erreur():
    script = """- Plan 1 (0-12s)
- Video: A
- Video: B
- Video: C
- VO: Short.
"""
    errs = pacing_errors(script)
    assert any("Trop de vidéos" in e and "max 2 par plan" in e for e in errs), errs


def test_rewrite_timestamps_sequential():
    script = """# HOOK (0-20s)
- Plan 1 (0-3s)
- Video: A
- VO: Short.
- Plan 2 (10-12s)
- Video: B
- VO: Again short.
# BODY (0-20s)
- Plan 3 (3-30s)
- Video: C
- VO: Way too long for me yeah this is way too long indeed.
- Plan 4 (9-10s)
- Video: D
- VO: Small.
"""
    out = rewrite_plan_timestamps(script)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    # Plan 1 = 1e Video = I2V (3s) ; Plan 2 = 1e Video = I2V (3s) ; P3/P4 = T2V (4s)
    assert "- Plan 1 (0-3s)" in lines
    assert "- Plan 2 (3-6s)" in lines
    assert "- Plan 3 (6-10s)" in lines
    assert "- Plan 4 (10-14s)" in lines
    assert "# BODY (6-14s)" in lines
    # nombre de plans et sections conservé
    assert sum(1 for l in lines if l.startswith("- Plan")) == 4


def test_repair_pacing_ajoute_2e_asset_sans_couper():
    # VO 14 mots sur un plan 1 asset (3s, budget 10) -> repair ajoute la 2e
    # vidéo (7s, budget 18) sans jamais couper la phrase.
    script = """- Plan 1 (0-3s)
- Video: The black-armored swordswoman advancing
- VO: If your favorite is Ruan Mei I have bad news indeed.
"""
    fixed = repair_pacing_script(script)
    assert fixed is not None
    errs = pacing_errors(fixed)
    assert errs == [], errs
    # jamais de phrase coupée : l'ajout d'assets ne remplace jamais la VO
    assert "If your favorite is Ruan Mei I have bad news indeed." in fixed
    plans = parse_plans(fixed)
    assert len(plans[0]["video_lines"]) == 2


def test_repair_retime_segments_contigus():
    script = """- Plan 1 (0-3s)
- Video: A
- VO: A very long sentence that absolutely will not fit in three seconds at all.
- Plan 2 (3-7s)
- Video: B
- VO: Short.
"""
    fixed = repair_pacing_script(script)
    assert fixed is not None
    p1 = parse_plans(fixed)[0]
    assert len(p1["video_lines"]) >= 2
    out = rewrite_plan_timestamps(fixed)
    plans = parse_plans(out)
    # contiguïté stricte : fin d'un plan = début du suivant
    for a, b in zip(plans, plans[1:]):
        assert abs(a["end_s"] - b["start_s"]) < 0.01, (a, b)


def test_repair_impossible_depasse_cap_2_assets():
    script = """- Plan 1 (0-3s)
- Video: A
- VO: This sentence is simply far too long to ever fit inside one single plan of three seconds because it keeps rambling on and on without any end in sight my friend seriously and then it keeps talking for several more words forever.
"""
    # phrase intenable même sur 2 vidéos (cap) : repair retourne None
    fixed = repair_pacing_script(script)
    assert fixed is None


def test_repair_retire_3e_video():
    script = """- Plan 1 (0-11s)
- Video: A
- Video: B
- Video: C
- VO: The banner just dropped.
"""
    fixed = repair_pacing_script(script)
    assert fixed is not None
    plans = parse_plans(fixed)
    assert len(plans[0]["video_lines"]) == 2
    # durées mises à jour = 2 assets (I2V+T2V = 7s)
    out = rewrite_plan_timestamps(fixed)
    assert "- Plan 1 (0-7s)" in [l.strip() for l in out.splitlines()]


def test_vo_18_mots_ok_sur_2_assets():
    # 18 mots dans un plan 2 assets (7s, budget 18) -> pas d'erreur de pacing
    script = """- Plan 1 (0-7s)
- Video: A
- Video: B
- VO: One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen.
"""
    errs = pacing_errors(script)
    assert errs == [], errs


def test_vo_21_mots_rejetee_cap_global():
    # 21 mots = au-delà du filet absolu (20) : GeneratedScriptAlt rejette
    script = """Audio: UPBEAT_GAMING
# HOOK (0-4s)
- Plan 1 (0-4s)
- Video: One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone.
- VO: One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone.
# BODY (4-20s)
- Plan 2 (4-8s)
- Video: B
- VO: Plan two voice over.
- Plan 3 (8-12s)
- Video: C
- VO: Plan three voice over.
- Plan 4 (12-16s)
- Video: D
- VO: Plan four voice over.
- Plan 5 (16-20s)
- Video: E
- VO: Plan five voice over.
# CTA (20-24s)
- Plan 6 (20-24s)
- Video: F
- VO: Plan six voice over.
- Plan 7 (24-28s)
- Video: G
- VO: Plan seven voice over.
"""
    try:
        GeneratedScriptAlt(script=script)
        assert False, "un VO de 21 mots aurait dû être rejeté"
    except Exception as e:
        assert "beaucoup trop longue" in str(e), e


def test_plan_trois_videos_rejete():
    script = """Audio: UPBEAT_GAMING
# HOOK (0-11s)
- Plan 1 (0-11s)
- Video: A
- Video: B
- Video: C
- VO: Short but three videos here.
# BODY (11-27s)
- Plan 2 (11-15s)
- Video: D
- VO: Plan two voice over.
- Plan 3 (15-19s)
- Video: E
- VO: Plan three voice over.
- Plan 4 (19-23s)
- Video: F
- VO: Plan four voice over.
# CTA (23-27s)
- Plan 5 (23-27s)
- Video: G
- VO: Plan five voice over.
- Plan 6 (27-31s)
- Video: H
- VO: Plan six voice over.
- Plan 7 (31-35s)
- Video: I
- VO: Plan seven voice over.
"""
    try:
        GeneratedScriptAlt(script=script)
        assert False, "un plan à 3 vidéos aurait dû être rejeté"
    except Exception as e:
        assert "maximum 2 vidéos" in str(e), e


def test_parse_plans_indices_video():
    plans = parse_plans(SCRIPT_7S)
    p1 = plans[0]
    assert [v["idx"] for v in p1["video_lines"]] == [0, 1]
    assert p1["section"] == "hook"
    assert len(plans) >= 2


if __name__ == "__main__":
    test_regle_i2v_2_assets_seulement()
    test_plan_requis_7s_pour_2_assets()
    test_pacing_ok_7s()
    test_vo_trop_longue_detectee()
    test_horaires_incoherents_detectes()
    test_budget_parole_grandit_avec_assets()
    test_budget_parole_sature_a_2_assets()
    test_plan_plus_de_2_videos_erreur()
    test_rewrite_timestamps_sequential()
    test_repair_pacing_ajoute_2e_asset_sans_couper()
    test_repair_retime_segments_contigus()
    test_repair_impossible_depasse_cap_2_assets()
    test_repair_retire_3e_video()
    test_vo_18_mots_ok_sur_2_assets()
    test_vo_21_mots_rejetee_cap_global()
    test_plan_trois_videos_rejete()
    test_parse_plans_indices_video()
    print("test_script_timing PASSED")