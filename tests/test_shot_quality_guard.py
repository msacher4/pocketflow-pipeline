"""Tests du guard de qualité de plan (helpers/shot_quality_guard.py) et de son
branchement dans la validation ALT.

Deux questions posées EN UNE SEULE requête JEV sur chaque ligne `Video:` :
  (a) physique — le mouvement demandé est-il réalisable ou ça va morphiner ;
  (b) fidélité — l'image montre-t-elle simplement l'idée de la VO.

Aucun LLM, aucun GPU, aucun appel réseau — JEV est mocké. Lancement :
    /usr/bin/python3 tests/test_shot_quality_guard.py
"""
import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from helpers.shot_quality_guard import (
    PHYSICS_BLOCKING,
    PHYSICS_QUESTION,
    VO_MATCH_BLOCKING,
    VO_MATCH_QUESTION,
    classify_shot,
)
from nodes.scriptwriter.pydantic_validation import (
    AltPydanticScriptValidationNode,
    is_asset_error,
)

log = logging.getLogger("pocketflow-pipeline")

failures: list[str] = []


def _asyncify(fn):
    async def wrapper(*a, **k):
        return fn(*a, **k)
    return wrapper


# --------------------------------------------------------------------------
# Les questions JEV
# --------------------------------------------------------------------------

def test_question_physique_a_les_deux_critères():
    assert PHYSICS_QUESTION["type"] == "choice"
    assert set(PHYSICS_QUESTION["criteria"]) == {"plausible", "implausible"}
    assert PHYSICS_BLOCKING in PHYSICS_QUESTION["criteria"]


def test_critere_implausible_vise_objet_sans_agent():
    """Le cas du run du 06/10 : un archet qui glisse tout seul sur les cordes."""
    crit = PHYSICS_QUESTION["criteria"]["implausible"].lower()
    assert "no visible agent" in crit, "le critère doit nommer l'agent invisible"
    assert "itself" in crit or "on its own" in crit


def test_critere_plausible_laisse_passer_camera_sur_scene_immobile():
    """Sans ça, un plan d'insert calme (4 plans sur 7) serait systématiquement
    renvoyé alors qu'un lent mouvement de caméra est parfaitement valide."""
    crit = PHYSICS_QUESTION["criteria"]["plausible"].lower()
    assert "camera moves" in crit
    assert "static scene" in crit


def test_question_vo_a_les_deux_critères():
    assert VO_MATCH_QUESTION["type"] == "choice"
    assert set(VO_MATCH_QUESTION["criteria"]) == {"related", "unrelated"}
    assert VO_MATCH_BLOCKING in VO_MATCH_QUESTION["criteria"]


def test_critere_unrelated_vise_un_autre_sujet_entier():
    crit = VO_MATCH_QUESTION["criteria"]["unrelated"].lower()
    assert "different topic entirely" in crit
    assert "mood, a colour or a tone is not enough" in crit, \
        "une simple ressemblance d'ambiance ne doit pas déclencher le rejet"


def test_critere_related_autorise_l_ambiance_pour_une_vo_abstraite():
    """Sans ça, la moitié des plans part en réécriture : une VO abstraite
    (« choisis ton camp ») est normalement illustrée par une atmosphère."""
    crit = VO_MATCH_QUESTION["criteria"]["related"].lower()
    assert "abstract voice-over" in crit
    assert "choose your side" in crit
    assert "not to another one" in crit


def test_soul_documente_les_quatre_erreurs():
    """Contrat guard <-> soul : le VideoAssetFixer doit savoir quoi faire des
    messages que le validateur lui envoie. Chaque contrôle a 2 issues (refus
    net et doute), le soul doit documenter les 4."""
    soul = (PIPELINE_ROOT / "souls" / "video_asset_fixer.md").read_text(
        encoding="utf-8").lower()
    for phrase in (
        "physiquement invraisemblable",
        "n'est pas confirmé réalisable",
        "l'image ne montre pas l'idée de la vo",
        "n'est pas confirmée",
    ):
        assert phrase in soul, f"le soul doit documenter : {phrase}"
    assert "i2v comprises" in soul, "le soul doit préciser que les I2V sont visées"


# --------------------------------------------------------------------------
# classify_shot — appel JEV, fail-open
# --------------------------------------------------------------------------

class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)


class _FakePost:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc, self.calls = resp, exc, []

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        if self._exc:
            raise self._exc
        return self._resp


_ANSWERS = {"physics": {"choice": "plausible", "confidence": 0.92},
            "vo_match": {"choice": "related", "confidence": 0.88}}


def test_une_seule_requete_pour_les_deux_questions():
    """Les deux questions voyagent ensemble : une requête par Video:, pas deux."""
    fake = _FakePost(_Resp({"answers": _ANSWERS}))
    with patch("config.OPENROUTER_API_KEY", "test-key"), \
         patch("helpers.headcount_guard._client", return_value=fake):
        out = asyncio.run(classify_shot("un violon", "vo du plan"))
    assert out["ok"] is True
    assert len(fake.calls) == 1, "une seule requête JEV pour les deux questions"
    sent = fake.calls[0]["json"]["questions"]
    assert set(sent) == {"physics", "vo_match"}
    state = fake.calls[0]["json"]["state"]
    assert state["shot_prompt"] == "un violon"
    assert state["voice_over"] == "vo du plan"


def test_reponse_lue_correctement():
    fake = _FakePost(_Resp({"answers": _ANSWERS}))
    with patch("config.OPENROUTER_API_KEY", "test-key"), \
         patch("helpers.headcount_guard._client", return_value=fake):
        out = asyncio.run(classify_shot("prompt"))
    assert out["physics"]["choice"] == "plausible"
    assert out["physics"]["confidence"] == 0.92
    assert out["vo_match"]["choice"] == "related"


def test_fail_sans_cle_api():
    with patch("config.OPENROUTER_API_KEY", ""):
        out = asyncio.run(classify_shot("prompt"))
    assert out["ok"] is False


def test_fail_sur_exception_reseau():
    fake = _FakePost(exc=ConnectionError("boom"))
    with patch("config.OPENROUTER_API_KEY", "test-key"), \
         patch("helpers.headcount_guard._client", return_value=fake):
        out = asyncio.run(classify_shot("prompt"))
    assert out["ok"] is False


def test_fail_sur_http_500():
    resp = _Resp({"error": "nope"})
    resp.status_code = 500
    with patch("config.OPENROUTER_API_KEY", "test-key"), \
         patch("helpers.headcount_guard._client", return_value=_FakePost(resp)):
        out = asyncio.run(classify_shot("prompt"))
    assert out["ok"] is False


def test_fail_sur_choix_inattendu():
    body = {"answers": {"physics": {"choice": "maybe", "confidence": 0.9},
                        "vo_match": _ANSWERS["vo_match"]}}
    with patch("config.OPENROUTER_API_KEY", "test-key"), \
         patch("helpers.headcount_guard._client", return_value=_FakePost(_Resp(body))):
        out = asyncio.run(classify_shot("prompt"))
    assert out["ok"] is False


# --------------------------------------------------------------------------
# Branchement dans la validation ALT
# --------------------------------------------------------------------------

# Script de base : même squelette que test_i2v_broll (7 plans, 2 I2V en tête),
# plan 5 remplacé par le cas réel du run du 06/10.
SCRIPT_BASE = """Audio: synthwave ambient lente, 105 BPM

### HOOK
Plan 1 (0-8s)
Video: Lilly alone, penche la tête vers la caméra. Cheveux roux ondulés, costume brodé or, fond violet flou. La lumière glisse sur son front. Elle cligne des yeux. Silence.
VO: Lilly, idole de Magical Sisters, jamais jouable.
Video: Une scène de théâtre vide, rideau bordeaux fermé. La caméra descend lentement. Rampes rouges allumées, poussière en suspension. Aucun personnage présent. Silence.
VO: son jeu est corrompu depuis toujours.
Plan 2 (8-12s)
Video: Lilly alone, déplie lentement une carte à jouer. La lumière rouge balaie son profil. Decor sombre derrière elle, la caméra zoome sur ses mains. Elle souffle. Silence.
VO: aujourd'hui, le jeu revient enfin.
Plan 3 (12-16s)
Video: Une manette de jeu usée posée à l'envers. La caméra parcourt le plastique. La fenêtre glisse dessus, le stick est cassé. Aucun personnage présent. Cliquetis.
VO: le fandom s'est divisé en deux.
Plan 4 (16-20s)
Video: Un couloir néon vide, la lumière magenta pulse. La caméra avance, le sol mouillé qui brille. Aucun personnage présent. Bourdonnement.
VO: choisis ton camp.
Plan 5 (20-24s)
Video: Un téléphone éteint sur un matelas défaisé. La caméra descend. La lumière bleue balaie le tissu. Aucun personnage présent. Silence.
VO: la version corrompue arrive.
Plan 6 (24-28s)
Video: Un escalier de service en béton. La caméra monte marche après marche. Néon vert qui vacille, poussière dans le faisceau. Aucun personnage présent.
VO: personne n'a rien vu.
Plan 7 (28-32s)
Video: Une pile de boîtes de jeu sur une étagère. La caméra descend. La lampe glisse sur les jaquettes. Aucun personnage présent. Cliquetis.
VO: rejoins la discussion.
"""

# Le cas réel : « Her calm voice lands the final damage » illustrée par un
# archet qui glisse seul sur un violon posé sur un bureau.
PLAN5_VIOLON = (
    "Video: A bow slides slowly across the strings of a violin lying on a dusty "
    "wooden desk. The camera drifts sideways. Cold blue light from a window. Silence.\n"
    "VO: Her calm voice lands the final damage."
)
PLAN5_ORIGINE = (
    "Video: Un téléphone éteint sur un matelas défaisé. La caméra descend. La "
    "lumière bleue balaie le tissu. Aucun personnage présent. Silence.\n"
    "VO: la version corrompue arrive."
)
SCRIPT_VIOLON = SCRIPT_BASE.replace(PLAN5_ORIGINE, PLAN5_VIOLON)
assert SCRIPT_VIOLON != SCRIPT_BASE, "le fixture plan 5 doit bien être remplacé"


def _shot(p_choice="plausible", p_conf=0.95, m_choice="related", m_conf=0.95):
    def fn(prompt, vo="", cn="", fr=""):
        return {"ok": True,
                "physics": {"choice": p_choice, "confidence": p_conf},
                "vo_match": {"choice": m_choice, "confidence": m_conf}}
    return fn


def _broll_all_ok(prompt, vo="", cn="", fr=""):
    return {"ok": True, "choice": "b_roll", "confidence": 0.95}


async def _valider(script, shot, broll=_broll_all_ok, headcount=None):
    shared = {
        "script": script,
        "selected_article": {"character": {"name": "Lilly",
                                           "franchise": "Magical Sisters"}},
        "steps": [],
        "format": "markdown",
    }
    hc = headcount or (lambda p: {"ok": True, "choice": "no_person", "confidence": 0.9})
    node = AltPydanticScriptValidationNode()
    await node.prep_async(shared)
    with patch("helpers.headcount_guard.classify_headcount", new=_asyncify(hc)), \
         patch("helpers.broll_guard.classify_broll", new=_asyncify(broll)), \
         patch("helpers.shot_quality_guard.classify_shot", new=_asyncify(shot)):
        return await node.exec_async(shared)


def test_script_qualite_ok_passe():
    out = asyncio.run(_valider(SCRIPT_BASE, _shot()))
    assert out["valid"] is True, out.get("error")


def test_physique_invraisemblable_rejette():
    out = asyncio.run(_valider(SCRIPT_BASE, _shot(p_choice="implausible")))
    assert out["valid"] is False
    assert "physiquement invraisemblable" in out["error"]


def test_physique_renvoie_vers_le_fixer():
    out = asyncio.run(_valider(SCRIPT_BASE, _shot(p_choice="implausible")))
    assert is_asset_error(out["error"]), \
        "le préfixe ASSET(plan N) doit router vers le VideoAssetFixer"


def test_vo_obscure_rejette():
    out = asyncio.run(_valider(SCRIPT_BASE, _shot(m_choice="unrelated")))
    assert out["valid"] is False
    assert "l'image ne montre pas l'idée de la VO" in out["error"]
    assert is_asset_error(out["error"])


def test_vo_obscure_message_rappelle_la_vo():
    out = asyncio.run(_valider(SCRIPT_BASE, _shot(m_choice="unrelated")))
    assert "VO est" in out["error"], "la VO fautive doit figurer dans le message"


def test_gate_inverse_physique_doute_bloque():
    """GATE INVERSÉ : un choix 'plausible' mais hésitant (conf 0.25) bloque —
    JEV n'y croit pas, le prompt ressortira aussi mal qu'un faux."""
    out = asyncio.run(_valider(
        SCRIPT_BASE, _shot(p_choice="plausible", p_conf=0.25)))
    assert out["valid"] is False
    assert "n'est pas confirmé réalisable" in out["error"]


def test_gate_inverse_vo_doute_bloque():
    """Le cas Kafka plan 4 : JEV répond 'related' mais conf 0.19 (< 0.8) → le
    lien est trop lâche pour autoriser le plan."""
    out = asyncio.run(_valider(
        SCRIPT_BASE, _shot(m_choice="related", m_conf=0.19)))
    assert out["valid"] is False
    assert "n'est pas confirmée" in out["error"]
    assert is_asset_error(out["error"])


def test_gate_inverse_conf_haute_passe_toujours():
    """plausible + related à conf >= 0.8 : aucun des deux contrôles ne bloque."""
    out = asyncio.run(_valider(
        SCRIPT_BASE, _shot(p_choice="plausible", p_conf=0.88,
                           m_choice="related", m_conf=0.82)))
    assert out["valid"] is True, out.get("error")


def test_jev_en_panne_ne_bloque_jamais():
    out = asyncio.run(_valider(
        SCRIPT_BASE, lambda *a, **k: {"ok": False, "reason": "HTTP 503"}))
    assert out["valid"] is True


def test_i2v_egalement_controle():
    """Le contrôle court sur TOUTES les Video:, y compris les 2 premières (I2V) :
    un ancrage qui ne porte pas l'idée de la VO est autant de perdu."""
    def shot(prompt, vo="", cn="", fr=""):
        if prompt.startswith("Lilly alone"):
            return {"ok": True,
                    "physics": {"choice": "implausible", "confidence": 0.97},
                    "vo_match": {"choice": "related", "confidence": 0.9}}
        return _shot()(prompt, vo, cn, fr)

    out = asyncio.run(_valider(SCRIPT_BASE, shot))
    assert out["valid"] is False
    assert "ASSET(plan 1)" in out["error"], "l'I2V du plan 1 doit être visé"
    assert is_asset_error(out["error"])


def test_ordre_physique_avant_vo():
    """Quand les deux échouent, la physique est signalée en premier (c'est la
    cause racine : un prompt irréalisable ne se répare pas en changeant le sujet)."""
    out = asyncio.run(_valider(
        SCRIPT_BASE, _shot(p_choice="implausible", m_choice="unrelated")))
    assert out["valid"] is False
    assert "physiquement invraisemblable" in out["error"]


# --------------------------------------------------------------------------
# Régression : le plan 5 du run du 06/10
# --------------------------------------------------------------------------

def test_fixture_violon_arrive_jusqu_aux_controles_jev():
    """Le fixture doit passer les validateurs pydantic AVANT mes contrôles.

    Sans ça, un prompt rejeté par `script_video_no_static` ('holds still')
    fait échouer la validation avec un message qui contient déjà
    'ASSET(plan 5)' — les tests ci-dessous 'passent' alors pour la mauvaise
    raison, avec classify_shot jamais appelé.
    """
    out = asyncio.run(_valider(SCRIPT_VIOLON, _shot()))
    assert out["valid"] is True, out.get("error")

def test_regression_plan5_violon_rejete_physique():
    """« A bow slides across the strings » sans main = l'objet s'anime seul."""
    def shot(prompt, vo="", cn="", fr=""):
        if "violin" in prompt:
            return {"ok": True,
                    "physics": {"choice": "implausible", "confidence": 0.96},
                    "vo_match": {"choice": "related", "confidence": 0.9}}
        return _shot()(prompt, vo, cn, fr)

    out = asyncio.run(_valider(SCRIPT_VIOLON, shot))
    assert out["valid"] is False, out.get("error")
    assert "ASSET(plan 5)" in out["error"]
    assert is_asset_error(out["error"])


def test_regression_plan5_violon_rejete_vo():
    """« Her calm voice lands the final damage » illustré par un violon posé
    sur un bureau : aucune lecture directe entre les deux."""
    def shot(prompt, vo="", cn="", fr=""):
        if "violin" in prompt:
            return {"ok": True,
                    "physics": {"choice": "plausible", "confidence": 0.9},
                    "vo_match": {"choice": "unrelated", "confidence": 0.94}}
        return _shot()(prompt, vo, cn, fr)

    out = asyncio.run(_valider(SCRIPT_VIOLON, shot))
    assert out["valid"] is False, out.get("error")
    assert "ASSET(plan 5)" in out["error"]
    assert "final damage" in out["error"], "la VO doit être rappelée au réparateur"


def test_regression_plan5_la_vo_est_bien_lue():
    """Le validateur doit bien transmettre la VO du plan au guard."""
    seen = []

    def shot(prompt, vo="", cn="", fr=""):
        seen.append(vo)
        return _shot()(prompt, vo, cn, fr)

    asyncio.run(_valider(SCRIPT_VIOLON, shot))
    assert any("final damage" in v for v in seen), \
        f"VO jamais transmise, reçues : {seen}"


# --------------------------------------------------------------------------
# Régression : le script Kafka du 07/10 — plans 4 et 6
# JEV a répondu « related » mais avec conf 0.19 / 0.24, donc en DESSOUS du
# seuil. Le gate inversé doit les bloquer (« liaison non confirmée »).
# --------------------------------------------------------------------------

def test_regression_kafka_plan4_vo_doute_bloque():
    """Plan 4 : « Her calm teasing voice makes you feel owned » illustré par
    un avant-bras ganté sur une borne d'arcade — JEV 'related' 0.19 → doute."""

    def shot(prompt, vo="", cn="", fr=""):
        if "néon" in prompt.lower():
            return {"ok": True,
                    "physics": {"choice": "plausible", "confidence": 0.96},
                    "vo_match": {"choice": "related", "confidence": 0.19}}
        return _shot()(prompt, vo, cn, fr)

    out = asyncio.run(_valider(SCRIPT_BASE, shot))
    assert out["valid"] is False, out.get("error")
    assert "ASSET(plan 4)" in out["error"]
    assert "n'est pas confirmée" in out["error"]
    assert is_asset_error(out["error"])


def test_regression_kafka_plan6_vo_doute_bloque():
    """Plan 6 : « Evelyn held that spot, but Kafka may steal it » illustré par
    deux bornes d'arcade — JEV 'related' 0.24 → doute."""

    def shot(prompt, vo="", cn="", fr=""):
        if "escalier" in prompt.lower():
            return {"ok": True,
                    "physics": {"choice": "plausible", "confidence": 1.0},
                    "vo_match": {"choice": "related", "confidence": 0.24}}
        return _shot()(prompt, vo, cn, fr)

    out = asyncio.run(_valider(SCRIPT_BASE, shot))
    assert out["valid"] is False, out.get("error")
    assert "ASSET(plan 6)" in out["error"]
    assert "n'est pas confirmée" in out["error"]
    assert is_asset_error(out["error"])


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"FAILED {fn.__name__}: {e}")
        else:
            log.info("PASSED %s", fn.__name__)
    if failures:
        print(f"{len(failures)}/{len(tests)} FAILED")
        return 1
    print(f"ALL PASSED ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
