"""Tests du découpage I2V/T2V (helpers/i2v_slots.py) et de son usage concordant
par les 3 appelants : script_timing (durée de plan), AssetPlannerAlt (mode du
slot), PydanticScriptValidation ALT (contrôle JEV b-roll).

Aucun LLM, aucun GPU, aucun appel réseau — JEV est mocké. Lancement :
    /usr/bin/python3 tests/test_i2v_broll.py
"""
import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import patch

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from helpers.broll_guard import FACIAL_FEATURE_RE, is_facial, is_true_broll
from helpers.i2v_slots import I2V_SLOT_COUNT, i2v_slot_positions
from nodes.assetfinder.asset_planner import AssetPlannerAltNode
from nodes.scriptwriter.script_timing import (
    I2V_SEC,
    T2V_SEC,
    asset_duration_of,
    asset_kind_of,
    parse_plans,
    plan_required_duration,
    repair_pacing_script,
    to_broll_variant,
)
from nodes.scriptwriter.pydantic_validation import (
    AltPydanticScriptValidationNode,
    is_asset_error,
)

log = logging.getLogger("pocketflow-pipeline")


# --------------------------------------------------------------------------
# helpers/i2v_slots — la règle
# --------------------------------------------------------------------------

def test_i2v_premier_plan_avec_video():
    assert i2v_slot_positions([1, 2, 3]) == {0, 1}


def test_i2v_jamais_deux_dans_le_meme_plan():
    """Plan 1 avec 2 vidéos : seule la 1re est I2V, la 2e reste T2V."""
    assert i2v_slot_positions([1, 1, 2, 3]) == {0, 2}


def test_i2v_t2v_intercale_entre_les_deux_i2v():
    """Le cas signalé : VO longue au hook -> un T2V entre les deux I2V."""
    positions = i2v_slot_positions([1, 1, 2])
    assert positions == {0, 2}
    assert 1 not in positions, "le T2V intercalé ne doit pas être classé I2V"


def test_i2v_plan_sans_video_passe_aux_plans_suivants():
    """Hook purement textuel : le Plan 1 n'a aucun slot I2V."""
    assert i2v_slot_positions([2, 3, 4]) == {0, 1}


def test_i2v_trois_plans_a_deux_videos():
    assert i2v_slot_positions([1, 1, 2, 2, 3, 3]) == {0, 2}


def test_i2v_moins_de_deux_videos_disponibles():
    assert i2v_slot_positions([1]) == {0}
    assert i2v_slot_positions([]) == set()


def test_i2v_plan_index_illisible_ne_bloque_pas():
    """plan_index=None (en-tête illisible) : chaque ligne est un plan à part."""
    assert i2v_slot_positions([None, None, 2]) == {0, 1}


def test_i2v_count_zero():
    assert i2v_slot_positions([1, 2, 3], 0) == set()


def test_i2v_slot_count_valeur():
    assert I2V_SLOT_COUNT == 2


# --------------------------------------------------------------------------
# script_timing — durée de plan cohérente avec le découpage
# --------------------------------------------------------------------------

# 7 plans, durées = Σ assets. I2V = T2V = 4s (mêmes frames, même fps LTX-2.5).
# Le Plan 1 a 2 vidéos (VO longue) : sa 2e vidéo est un T2V PLACÉ ENTRE les
# deux I2V.
SCRIPT_T2V_INTERcale = """Audio: synthwave ambient lente, 105 BPM

### HOOK
Plan 1 (0-8s)
Video: Lilly alone, penche la tête vers la caméra. Cheveux roux ondulés, costume brodé or, fond violet flou. La lumière glisse sur son front. Elle cligne des yeux. Silence.
VO: Lilly, idole de Magical Sisters, jamais jouable.
Video: Une scène de théâtre vide, rideau bordeaux fermé. La caméra descend lentement. Rampes rouges allumées, poussière en suspension. Aucun personnage présent. Silence.
VO: son jeu est corrompu depuis toujours.
Plan 2 (8-12s)
Video: Lilly alone, déplie lentement une carte à jouer. La lumière rouge balaie son profil. Décor sombre derrière elle, la caméra zoome sur ses mains. Elle souffle. Silence.
VO: aujourd'hui, le jeu revient enfin.
Plan 3 (12-16s)
Video: Une manette de jeu usée posée à l'envers. La caméra parcourt le plastique. La fenêtre glisse dessus, le stick est cassé. Aucun personnage présent. Cliquetis.
VO: le fandom s'est divisé en deux.
Plan 4 (16-20s)
Video: Un couloir néon vide, la lumière magenta pulse. La caméra avance, le sol mouillé qui brille. Aucun personnage présent. Bourdonnement.
VO: choisis ton camp.
Plan 5 (20-24s)
Video: Un téléphone éteint sur un matelas défaisé. La caméra descend. La lumière bleue balaie le tissu. Aucun personnage présent. Silence.
VO: la version corrompée arrive.
Plan 6 (24-28s)
Video: Un escalier de service en béton. La caméra monte marche après marche. Néon vert qui vacille, poussière dans le faisceau. Aucun personnage présent.
VO: personne n'a rien vu.
Plan 7 (28-32s)
Video: Une pile de boîtes de jeu sur une étagère. La caméra descend. La lampe glisse sur les jaquettes. Aucun personnage présent. Cliquetis.
VO: rejoins la discussion.
"""


def test_timing_annote_i2v_et_t2v():
    """8 lignes Video: (le Plan 1 en a 2) : i2v, t2v, i2v, puis 5 t2v."""
    plans = parse_plans(SCRIPT_T2V_INTERcale)
    kinds = [v["kind"] for p in plans for v in p["video_lines"]]
    assert kinds == ["i2v", "t2v", "i2v",
                     "t2v", "t2v", "t2v", "t2v", "t2v"]
    assert len(kinds) == 8


def test_timing_plan_avec_t2v_intercale_dure_i2v_plus_t2v():
    plans = parse_plans(SCRIPT_T2V_INTERcale)
    p1 = plans[0]
    assert [v["kind"] for v in p1["video_lines"]] == ["i2v", "t2v"]
    assert plan_required_duration(p1) == I2V_SEC + T2V_SEC


def test_timing_plan_2_est_i2v_pas_t2v():
    """Régression : l'ancienne règle (plan_num in (1,2)) marquait 2 I2V dans le
    même plan dès que le Plan 1 avait 2 vidéos, et n'en marquait qu'un seul si le
    Plan 1 n'en avait aucune."""
    plans = parse_plans(SCRIPT_T2V_INTERcale)
    p2 = plans[1]
    assert asset_kind_of(p2, p2["video_lines"][0]) == "i2v"
    assert plan_required_duration(p2) == I2V_SEC


def test_timing_plan_sans_video_decale_le_decoupage():
    script = """Audio: synthwave ambient lente, 105 BPM

### HOOK
Plan 1 (0-4s)
Video: Une carte à jouer glisse sur une table en bois. La caméra descend, la fenêtre glisse dessus. Aucun personnage présent. Cliquetis.
VO: un plan textuel.
Plan 2 (4-10s)
Video: Lilly alone, lève les yeux vers la caméra. La lumière rouge balaie son profil. La caméra zoome lentement, décor sombre. Elle inspire. Silence.
VO: Lilly est de retour.
Plan 3 (10-14s)
Video: Une pile de boîtes de jeu sur une étagère. La caméra descend, la lampe glisse dessus. Aucun personnage présent. Cliquetis.
VO: le jeu revient.
Plan 4 (14-18s)
Video: Un couloir néon vide, la lumière magenta pulse. La caméra avance. Aucun personnage présent. Bourdonnement.
VO: un choix.
Plan 5 (18-22s)
Video: Un rideau de scène qui bouge, la caméra monte. La poussière flotte. Aucun personnage présent. Silence.
VO: une attente.
Plan 6 (22-26s)
Video: Des manettes de jeu alignées sur une table. La caméra glisse, la fenêtre éclaire. Aucun personnage présent. Cliquetis.
VO: deux camps.
Plan 7 (26-30s)
Video: Un écran éteint sur un matelas défaisé. La caméra descend, la lumière bleue balaie le tissu. Aucun personnage présent.
VO: rejoins la discussion.
"""
    plans = parse_plans(script)
    kinds = [v["kind"] for p in plans for v in p["video_lines"]]
    # Le Plan 1 a une vidéo et c'est un décor vide : c'est donc le 1er plan AVEC
    # une vidéo -> il prend le 1er I2V, et le Plan 2 le 2e. Un T2V au hook n'existe
    # que si le Plan 1 n'a aucune vidéo du tout.
    assert kinds == ["i2v", "i2v", "t2v", "t2v", "t2v", "t2v", "t2v"]


def test_timing_hook_sans_video_decale_le_premier_i2v():
    """Si le Plan 1 n'a AUCUNE ligne Video:, le 1er I2V tombe sur le Plan 2.
    C'est le bug du hook purement textuel que la règle des 'premiers plans qui ont
    une vidéo' corrige."""
    script = """Audio: synthwave ambient lente, 105 BPM

### HOOK
Plan 1 (0-4s)
Video: Une carte à jouer glisse sur une table, la caméra descend, aucun personnage présent. La fenêtre glisse dessus. Cliquetis.
VO: un plan.
"""
    plans = parse_plans(script)
    assert [v["kind"] for p in plans for v in p["video_lines"]] == ["i2v"]


def test_asset_duration_of_repli_sans_annotation():
    """Un plan construit à la main (non annoté) retombe sur l'ancienne règle."""
    plan = {"num": 1, "video_lines": [{"idx": 0}]}
    assert asset_kind_of(plan, {"idx": 0}) == "i2v"
    assert asset_duration_of(plan, {"idx": 0}) == I2V_SEC
    assert asset_kind_of(plan, {"idx": 1}) == "t2v"


# --------------------------------------------------------------------------
# Accord entre les 3 appelants
# --------------------------------------------------------------------------

def test_asset_planner_et_script_timing_concordent():
    plans = parse_plans(SCRIPT_T2V_INTERcale)
    flat = [(p["num"], v["idx"], v["kind"]) for p in plans for v in p["video_lines"]]

    node = AssetPlannerAltNode.__new__(AssetPlannerAltNode)
    decision = {"slots": [
        {"id": i + 1, "type": "visual", "plan_index": pn}
        for i, (pn, _, _) in enumerate(flat)
    ]}
    node._decorate_blueprint(
        {"selected_article": {"character": {"name": "Lilly"}}}, decision)

    planner = [s.get("mode", "t2v").lower() for s in decision["slots"]]
    assert planner == [k for _, _, k in flat]


def test_asset_planner_t2v_intercale():
    node = AssetPlannerAltNode.__new__(AssetPlannerAltNode)
    decision = {"slots": [
        {"id": 1, "type": "visual", "plan_index": 1},
        {"id": 2, "type": "visual", "plan_index": 1},
        {"id": 3, "type": "visual", "plan_index": 2},
    ]}
    node._decorate_blueprint(
        {"selected_article": {"character": {"name": "Lilly"}}}, decision)
    assert [s.get("mode", "t2v") for s in decision["slots"]] == ["i2v", "t2v", "i2v"]


# --------------------------------------------------------------------------
# Validation JEV b-roll
# --------------------------------------------------------------------------

def _asyncify(fn):
    async def wrapper(*a, **k):
        return fn(*a, **k)
    return wrapper


async def _valider(script, broll, headcount=None):
    shared = {
        "script": script,
        "selected_article": {"character": {"name": "Lilly",
                                           "franchise": "Magical Sisters"}},
        "steps": [],
        "format": "markdown",
    }
    hc = headcount or (lambda p: {"ok": True, "choice": "no_person", "confidence": 0.9})
    node = AltPydanticScriptValidationNode()
    prep = await node.prep_async(shared)
    with patch("helpers.headcount_guard.classify_headcount", new=_asyncify(hc)), \
         patch("helpers.broll_guard.classify_broll", new=_asyncify(broll)):
        return await node.exec_async(shared)


def _broll_all_ok(prompt, vo="", cn="", fr=""):
    return {"ok": True, "choice": "b_roll", "confidence": 0.95}


def test_broll_script_valide_passe():
    out = asyncio.run(_valider(SCRIPT_T2V_INTERcale, _broll_all_ok))
    assert out["valid"] is True, out.get("error")


def test_broll_t2v_avec_personnage_rejete_et_route_vers_fixer():
    def broll(prompt, vo="", cn="", fr=""):
        if "manette" in prompt:
            return {"ok": True, "choice": "character_visible", "confidence": 0.93}
        return {"ok": True, "choice": "b_roll", "confidence": 0.95}

    out = asyncio.run(_valider(SCRIPT_T2V_INTERcale, broll))
    assert out["valid"] is False
    err = out["error"]
    assert is_asset_error(err), "le préfixe ASSET(plan N) route vers le VideoAssetFixer"
    assert "T2V" in err and "b-roll" in err


def test_broll_t2v_intercale_vient_pourri_par_personnage():
    """Le contrôle doit viser le T2V intercalé du Plan 1, pas le plan suivant."""
    def broll(prompt, vo="", cn="", fr=""):
        if "scène de théâtre" in prompt:
            return {"ok": True, "choice": "character_visible", "confidence": 0.91}
        return {"ok": True, "choice": "b_roll", "confidence": 0.95}

    out = asyncio.run(_valider(SCRIPT_T2V_INTERcale, broll))
    assert out["valid"] is False
    assert is_asset_error(out["error"])


def test_broll_i2v_jamais_controle():
    """Même si JEV signale un I2V comme 'character_visible', on ne bloque pas :
    c'est précisément le personnage qu'on veut sur les 2 ancrages."""
    def broll(prompt, vo="", cn="", fr=""):
        if "Lilly" in prompt:
            return {"ok": True, "choice": "character_visible", "confidence": 0.99}
        return {"ok": True, "choice": "b_roll", "confidence": 0.95}

    out = asyncio.run(_valider(SCRIPT_T2V_INTERcale, broll))
    assert out["valid"] is True, out.get("error")


def test_broll_seuil_confiance_0_8():
    def broll(prompt, vo="", cn="", fr=""):
        if "manette" in prompt:
            return {"ok": True, "choice": "character_visible", "confidence": 0.79}
        return {"ok": True, "choice": "b_roll", "confidence": 0.95}

    out = asyncio.run(_valider(SCRIPT_T2V_INTERcale, broll))
    assert out["valid"] is True, "sous 0.8 on laisse passer"


def test_broll_jev_en_panne_ne_bloque_jamais():
    out = asyncio.run(_valider(
        SCRIPT_T2V_INTERcale, lambda *a, **k: {"ok": False, "reason": "HTTP 503"}))
    assert out["valid"] is True


def test_broll_message_inclut_la_vo_du_plan():
    """Le message doit rappeler l'idée à illustrer pour aider la réparation."""
    def broll(prompt, vo="", cn="", fr=""):
        if "manette" in prompt:
            return {"ok": True, "choice": "character_visible", "confidence": 0.95}
        return {"ok": True, "choice": "b_roll", "confidence": 0.95}

    out = asyncio.run(_valider(SCRIPT_T2V_INTERcale, broll))
    assert out["valid"] is False
    assert "fandom" in out["error"], "la VO fautive doit figurer dans le message"


def test_broll_question_bannit_detail_de_visage_et_reflet():
    """Contrat guard <-> soul sur la frontière œil/mains.

    Un œil en close-up et un visage en reflet doivent être rangés du côté
    `character_visible`, sinon JEV renvoie en correction des plans que le soul
    autorise (et inversement). Les mains, elles, restent du bon côté.
    """
    from helpers.broll_guard import BROLL_QUESTION
    cv = BROLL_QUESTION["criteria"]["character_visible"].lower()
    br = BROLL_QUESTION["criteria"]["b_roll"].lower()

    assert "eye" in cv, "le détail de visage (œil) doit être character_visible"
    assert "facial feature" in cv, "les features de visage doivent être bannies"
    assert "reflection" in cv, "un visage en reflet doit être character_visible"

    # frontière cohérente : b_roll tolère les mains mais pas les features de visage
    assert "hands" in br, "les mains restent autorisées en b-roll"
    assert "never a facial feature" in br, (
        "b_roll doit exclure explicitement les features de visage, sinon la "
        "frontière avec character_visible est ambiguë"
    )


def test_broll_soul_et_guard_forment_la_meme_interdiction():
    """Le soul doit afficher la même liste noircie que les critères JEV."""
    from pathlib import Path

    from helpers.broll_guard import BROLL_QUESTION
    cv = BROLL_QUESTION["criteria"]["character_visible"].lower()
    soul = (Path(__file__).resolve().parent.parent / "souls"
            / "script_generator_alt.md").read_text(encoding="utf-8").lower()

    # le soul cite explicitement l'œil et le reflet, comme le guard
    assert "détail de visage" in soul, "le soul doit bannir les détails de visage"
    assert "en reflet" in soul, "le soul doit bannir le visage en reflet"
    assert "mains" in soul, "le soul doit continuer d'autoriser les mains"

    # et il conserve la frontière main-autorisée / visage-interdit
    assert "eye" in cv and "mains" in soul


# --------------------------------------------------------------------------
# headcount_guard — classe no_person (nécessaire aux inserts b-roll)
# --------------------------------------------------------------------------

def test_headcount_question_expose_no_person():
    from helpers.headcount_guard import HEADCOUNT_CHOICES, HEADCOUNT_QUESTION
    assert "no_person" in HEADCOUNT_QUESTION["criteria"]
    assert "no_person" in HEADCOUNT_CHOICES


def test_headcount_no_person_ne_recoit_jamais_alone():
    """Un insert de scène vide ne doit jamais être traité comme one_person."""
    from helpers.headcount_guard import apply_headcount_guard
    prompt, _neg, meta = apply_headcount_guard(
        "empty theatre stage, red stage lights", None,
        {"ok": True, "choice": "no_person", "confidence": 0.95})
    assert "alone" not in prompt.lower()
    assert meta["mode"] == "no_person"


# --------------------------------------------------------------------------
# 2e asset : le filet de réparation doit produire un VRAI b-roll distinct
#
# Régession : le run 20261005_195636 avait 5 plans sur 7 où le 2e asset était
# une copie littérale suffixée "— alternate camera angle 1" (~6 min de GPU
# gaspillées par copie) — et, sur le Plan 1, une copie d'un plan Saber Alter
# devenue un T2V interdit.
# --------------------------------------------------------------------------

_SABER_BASE = (
    "Saber Alter in her black armored dress with silver hair and glowing red "
    "eyes, in a dark ruined castle hall lit by cold blue moonlight, raising "
    "her blackened sword and slamming it into the stone floor, red corruption "
    "mist curling around her, slow push-in on her defiant face, echoing wind, "
    "then a burst of crimson particles"
)
_V19_VO = (
    "If Saber Alter just became your waifu in Fate/EXTRA Record, I have bad "
    "news. You're a hopeless case."
)


def _repair_with_one_video(base: str, vo: str) -> list[str]:
    """Plan dont la VO (19 mots > budget 12) force l'ajout d'un 2e asset."""
    import re
    out = repair_pacing_script(f"Plan 1 (0-4s)\nVideo: {base}\nVO: {vo}\n")
    assert out, "le filet doit réparer ce plan"
    return [v.strip() for v in re.findall(r"^\s*Video\s*:\s*(.*)$", out, re.M)]


def test_repair_plus_jamais_de_duplicate_alternate_camera():
    """L'ancien fallback copiait `base` mot pour mot : plus jamais."""
    for base, vo in (
        (_SABER_BASE, _V19_VO),
        ("A dark arena, rows of empty seats, slow tilt upward",
         "The remake even brings new story routes and reimagined battles. "
         "That voice taunting you there? Sealed since day one."),
    ):
        vids = _repair_with_one_video(base, vo)
        assert len(vids) == 2, "la VO longue doit recevoir un 2e asset"
        assert vids[0] != vids[1], "le 2e asset ne doit JAMAIS être une copie"
        assert "alternate camera angle" not in vids[1]


def test_variant_plan_personnage_debarrasse_du_personnage():
    """Plan 1 : le 2e asset doit garder la scène, laisser tomber Saber Alter."""
    variant = to_broll_variant(_SABER_BASE, 1)
    assert variant
    assert "Saber" not in variant, "le T2V ne doit pas recruter le perso"
    assert not is_facial(variant), "aucun détail de visage sur un T2V"
    assert is_true_broll(variant)


def test_variant_conserve_le_decor_de_la_scene():
    """On ne change pas de décor : le b-roll reste dans le plan concerné."""
    variant = to_broll_variant(_SABER_BASE, 1)
    assert "dark ruined castle hall" in variant, "le décor doit survivre"
    assert "crimson particles" in variant, "l'ambiance doit survivre"


def test_variant_mains_autorisees_visage_interdit():
    """Frontière Marc : mains OK, yeux/visage non — y compris en reflet."""
    hands = ("Close-up of a hand flipping through a worn Fate visual novel "
             "collector's box on a cluttered desk, warm desk lamp light, "
             "fingers stopping on a page, slow zoom in, pages rustling softly")
    variant = to_broll_variant(hands, 1)
    assert variant and "hand" in variant, "les mains restent autorisées"
    assert not is_facial(variant)
    # interdits
    assert is_facial("close-up on her iris and eyelid")
    assert is_facial("her face reflecting in the screen")
    # pas un visage
    assert not is_facial("a smartphone lying face-up")
    assert not is_facial("a phone lying face down")


def test_variant_deux_ajouts_successifs_distincts():
    """Ajouter 2e puis 3e asset ne doit pas donner deux fois la même ligne."""
    base = "A dark arena, rows of empty seats, slow tilt upward"
    assert to_broll_variant(base, 1) != to_broll_variant(base, 2)


def test_variant_personnage_sans_decor_bascule_sur_insert_neutre():
    """base 100 % personnage, pas un mot de décor : on ne peut pas en tirer un
    b-roll fidèle à la scène. On retombe sur un insert d'ambiance NEUTRE plutôt
    que de couper la VO — `repair_pacing_script` promet de ne jamais raccourcir
    un texte pour compenser un ajout, et l'insert passe le contrôle JEV."""
    variant = to_broll_variant("Saber Alter raising her blackened sword", 1)
    assert variant, "jamais None : un plan a toujours un 2e asset possible"
    assert is_true_broll(variant)
    assert "Saber" not in variant


def test_variant_un_plan_sans_video_recoit_quand_meme_un_asset():
    """Un plan doit avoir >= 1 vidéo : insert générique, jamais 0 asset."""
    gen = to_broll_variant("", 1)
    assert gen and is_true_broll(gen)


def test_repair_rend_none_sur_script_deja_propre():
    """Contrat « rien à réparer ». Régressé au passage : `splitlines()`
    mangeait le \\n final, `retimed != script` était toujours vrai et le
    filet ne rendait JAMAIS None."""
    clean = ("Plan 1 (0-4s)\nVideo: A dark arena, rows of empty seats, "
             "slow tilt upward.\nVO: Short line here.\n")
    assert repair_pacing_script(clean) is None


def test_repair_rend_none_sur_les_fixtures_du_run():
    """Les fixtures AssetFinder (7 plans, 48s) doivent être un point fixe."""
    from pathlib import Path
    fixture = (Path(__file__).resolve().parent / "Test assetfoinder alt"
               / "script").read_text(encoding="utf-8")
    assert repair_pacing_script(fixture) is None


def test_parite_regex_facial_et_criteres_jev():
    """FACIAL_FEATURE_RE doit couvrir les termes littéraux de BROLL_QUESTION,
    sans jamais faire basculer les mains de « acceptable » à interdit."""
    from helpers.broll_guard import BROLL_QUESTION
    cv = BROLL_QUESTION["criteria"]["character_visible"]
    br = BROLL_QUESTION["criteria"]["b_roll"]
    for terme in ("iris", "eyelid", "brow", "jaw"):
        assert terme in cv.lower(), f"le critère JEV doit citer {terme}"
        assert FACIAL_FEATURE_RE.search(terme), f"FACIAL_FEATURE_RE manque {terme}"
    assert "hands and gear are acceptable" in br.lower(), \
        "la frontière main-autorisée doit rester dans le critère JEV"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        log.info("PASSED %s", fn.__name__)
    print(f"ALL PASSED ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
