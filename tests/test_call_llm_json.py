"""Tests du réparateur JSON LLM dans helpers/call_llm._extract_json.

Cas critiques : guillemets doubles non échappés dans les valeurs string
(motif qui faisait échouer alt_script_generator sur qwen3.8-27b), sans
corruption des séquences d'échappement déjà valides (\\n, \\", \\\\).
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from helpers.call_llm import _extract_json, _repair_quotes_aggressive


def _accepte_guillemets(value: str) -> bool:
    return "guillemets doubles" in value or "guillemets" not in value.replace("'", "")


def test_json_propre_passe_par():
    raw = '{"script": "Hello world", "ok": true}'
    assert _extract_json(raw) == {"script": "Hello world", "ok": True}


def test_guillemets_bruts_simples():
    raw = '{"script": "Video: une sc\u00e8ne "scary" au centre"}'
    assert _extract_json(raw)["script"] == "Video: une sc\u00e8ne 'scary' au centre"


def test_guillemets_bruts_imbriques():
    raw = '{"script": "Elle a dit "watch out" puis "run!" cest tout"}'
    assert _extract_json(raw)["script"] == "Elle a dit 'watch out' puis 'run!' cest tout"


def test_guillemets_bruts_en_fin_de_valeur():
    raw = '{"script": "la derni\u00e8re citation est "ici""}'
    assert _extract_json(raw)["script"] == "la derni\u00e8re citation est 'ici'"


def test_agressive_reparre_sans_corrompre():
    raw = '{"script": "ca marcha "a" et "b" et "c""}'
    agg = _repair_quotes_aggressive(raw)
    res = _extract_json(agg)["script"]
    assert res == "ca marcha 'a' et 'b' et 'c'"


def test_newlines_litteraux_preserves():
    raw = '{"script": "Audio: UPBEAT_GAMING\\n\\n### HOOK (0-3s)\\nPlan 1\\nVideo: mouvement "rapide"\\n"}'
    res = _extract_json(raw)["script"]
    assert "\n\n### HOOK (0-3s)" in res
    assert 'mouvement "rapide"' in res or "mouvement 'rapide'" in res


def test_apostrophes_typo_ok():
    raw = '{\'script\': "Video: une sc\u00e8ne d\u2019horreur"}'
    assert _extract_json(raw)["script"] == "Video: une sc\u00e8ne d\u2019horreur"


def test_prose_apres_objet():
    raw = '{"script": "un "vrai" script"} Voici une explication qui suit.'
    assert _extract_json(raw)["script"] == "un 'vrai' script"


def test_echappements_deja_valides_doubles_backslash():
    raw = '{"script": "chemin C:\\\\temp et citation \\"ici\\""}'
    res = _extract_json(raw)["script"]
    assert res == "chemin C:\\temp et citation \"ici\""


def test_fences_json():
    raw = '```json\n{"script": "du contenu "fautif" a l\'interieur"}\n```'
    assert _extract_json(raw)["script"] == "du contenu 'fautif' a l'interieur"


def test_replay_reel_qwen():
    with open("/tmp/opencode/sw_missed_raw.txt") as f:
        raw = f.read()
    res = _extract_json(raw)
    assert "script" in res
    assert "Laurie Strode" in res["script"]
    assert "\\n### HOOK" not in res["script"]


def test_degrade_imparable_leve_erreur():
    try:
        _extract_json("pas un json du tout de type object {")
    except Exception:
        return
    raise AssertionError("un input non-JSON aurait du lever une exception")


if __name__ == "__main__":
    test_json_propre_passe_par()
    test_guillemets_bruts_simples()
    test_guillemets_bruts_imbriques()
    test_guillemets_bruts_en_fin_de_valeur()
    test_newlines_litteraux_preserves()
    test_apostrophes_typo_ok()
    test_prose_apres_objet()
    test_echappements_deja_valides_doubles_backslash()
    test_fences_json()
    test_replay_reel_qwen()
    test_degrade_imparable_leve_erreur()
    print("ALL CALL_LLM JSON TESTS PASSED")