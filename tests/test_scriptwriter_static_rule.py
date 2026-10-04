"""Test de la règle 12 — DYNAMISME OBLIGATOIRE (plan FIGÉ interdit).

Régression du run 20261004_131919 : la règle rejettait `frozen` sur TOUTE la
ligne `Video:`, donc y compris un DÉCOR (« a vast frozen throne hall ») alors
que le personnage était en mouvement. 6 régénérations AltSG, ~39 min, pour une
erreur identique à chaque cycle.

Le contrôle vit maintenant dans `script_timing.static_video_reason`, source
UNIQUE partagée entre le validateur pydantic et le ScriptFixer (leurs deux
copies locales avaient divergé).

Modèle validé avec l'user :
- C'est le PERSONNAGE qui doit bouger ; le décor a le droit d'être figé.
- Une ligne purement décorative (règle 6 du soul) est acceptée sans verbe.
- Le préfixe `ASSET(plan N):` est un INVARIANT DE ROUTING : `is_asset_error()`
  teste "asset(plan" pour envoyer vers le ScriptFixer ; sans lui l'erreur
  retombe dans le `else` → régénération AltSG complète.

Aucun LLM, aucun GPU — pure logique déterministe.
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from pydantic import ValidationError

from nodes.scriptwriter.script_timing import (
    character_present,
    has_dynamic_verb,
    static_video_reason,
)
from nodes.scriptwriter.pydantic_validation import (
    GeneratedScriptAlt,
    is_asset_error,
)
from nodes.scriptwriter.script_fixer import ScriptFixerNode
from nodes.scriptwriter.alt_script_generator import (
    _first_audio_value,
    _force_audio_value,
)

# Les 6 lignes RÉELLEMENT rejetées à tort le 04/10 (verbatim depuis les steps
# du run), complétées pour passer le gate de longueur (>=25 mots, >=3 phrases)
# sans quoi pydantic court-circuite AVANT la règle 12 et le test ne prouve rien.
# Toutes doivent passer : le personnage y est en mouvement.
FALSE_REJECTIONS = [
    "Video: The Tsaritsa from Genshin Impact strides alone through a vast "
    "frozen throne hall, ice crystals drifting in the air. Her coat snaps "
    "behind her. Pale light cuts across the stone.",
    "Video: The Tsaritsa from Genshin Impact strides forward inside an icy "
    "cathedral, her breath fogging in the cold. Chains rattle overhead. Blue "
    "shadows pool at her feet.",
    "Video: A hooded traveler archetype kneels in deep snow outside a "
    "towering frozen gate, hood whipping in the wind. Snow lifts around the "
    "knees. The gate groans on ancient hinges.",
    "Video: The Tsaritsa from Genshin Impact turns alone in a vast frozen "
    "throne hall, cape sweeping. Braziers gutter along the walls. Frost "
    "glitters across the marble.",
    "Video: The Tsaritsa from Genshin Impact spins alone across a vast frozen "
    "throne hall, blades drawn. Ice shards burst outward. The pillars blur "
    "past her.",
    "Video: An ice queen archetype on a frozen battlefield lowers her lance "
    "and extends it toward the horizon. Snow whips sideways. Distant banners "
    "snap in the gale.",
    # Exemple dynamique de la règle 12 du soul.
    "Video: A black-armored swordswoman striding through a shattered arena, "
    "cape whipping in dark wind, red embers drifting. She raises a blade "
    "overhead. The crowd roars and recedes.",
    # Lessive : `folds` est bien une action, pas une pose figée.
    "Video: A gentle anime girl with short hair and a red ribbon folds shirts "
    "alone in a quiet laundromat, sleeves rolling. Steam drifts past the "
    "racks. Sunlight stripes the wooden floor.",
]

# Le VRAI défaut que l'ancienne regex ne voyait pas : `stands alone` et
# `standing motionless` passaient, et donnaient une frame immobile que LTX
# anime en simple zoom.
TRUE_REJECTIONS = [
    "Video: A gentle anime laundress girl with short brown hair and a red "
    "hair ribbon stands alone in a cozy seaside laundromat at dusk. She wears "
    "a navy vest over a crisp white shirt. Warm amber light spills across the "
    "folding tables.",
    "Video: A soft-spoken young laundress with a red ribbon in her hair "
    "stands alone at a folding table in a warm evening laundromat. Steam "
    "curls from a copper pot behind her. She does not look up from the linen.",
    "Video: A young woman stands motionless at the top of an icy staircase, "
    "hands folded. Frost creeps along the banister. Pale light leaks through "
    "a cracked window behind her.",
    "Video: A young woman standing still at a bus stop, waiting. Rain "
    "streaks the shelter glass. A single streetlamp buzzes above the empty "
    "road.",
    "Video: A girl in a static shot, portrait centered, holding a neutral "
    "expression. Her hair falls evenly past her shoulders. The background "
    "fades to a flat grey.",
    "Video: A dancer remains motionless on an empty stage, arms at her "
    "sides. Her ribbons hang without lift. Footlights flare across the dark "
    "boards. The house seats stay empty.",
]

# Règle 6 du soul : « personnages, décors, actions, caméra, éclairage,
# ambiance » — une ligne purement décorative n'exige aucun verbe, même figé.
AMBIENT_OK = [
    "Video: An empty frozen lake at night, motionless ice reflecting a pale "
    "moon. A ridge of pines cuts the horizon. Thin fog creeps across the "
    "surface.",
    "Video: A cozy laundromat at dusk, warm amber light spilling over wooden "
    "benches. Folded towels rest in neat stacks. Steam curls above a copper "
    "pot.",
]


def _script_with_videos(videos: list[str]) -> str:
    """Script ALT minimal servant de squelette : Audio + les 3 sections
    (hook/body/cta) + 7 plans minimum exigés par le validateur. La 1re ligne
    Video testée est le plan 1."""
    filler = (
        "Video: A young woman strides forward through a crowded market, coat "
        "sweeping behind her, lanterns swinging overhead as she weaves between "
        "the stalls. Spices drift from a nearby cart. Voices rise around her."
    )
    lines = ["Audio: JRPG_BATTLE", "", "### HOOK (0-4s)"]
    for i, video in enumerate(videos, start=1):
        lines += [f"Plan {i} (0-4s)", video, "VO: She moves.", ""]
    nxt = len(videos) + 1
    for section, count in (("BODY", 4), ("CTA", 2)):
        end = nxt + count - 1
        lines += [f"### {section} (4-{4 * end}s)"]
        for _ in range(count):
            lines += [f"Plan {nxt} (4-{4 * end}s)", filler, "VO: She moves.", ""]
            nxt += 1
    return "\n".join(lines)


def _validator_static_error(video: str) -> str | None:
    """Fragment d'erreur « plan FIGÉ » émis par le VALIDATEUR pydantic, ou None.

    Isole la règle 12 des AUTRES règles (7 plans, sections, durée VO…) : le
    script squelette est volontairement minimal, on ne veut tester que le
    verdict de la règle « plan FIGÉ »."""
    try:
        GeneratedScriptAlt(script=_script_with_videos([video]))
    except ValidationError as exc:
        blob = str(exc)
        if "plan FIGÉ" not in blob:
            return None
        # Isole le message du validateur qui porte la règle 12.
        for part in blob.split("\n"):
            if "plan FIGÉ" in part:
                return part.strip()
        return blob
    return None


def _fixer_verdict(video: str) -> list[str]:
    """Erreurs résiduelles du ScriptFixer sur la même ligne."""
    return ScriptFixerNode()._self_validate(_script_with_videos([video]), {})


def main() -> int:
    failures: list[str] = []

    # --- 1. Plus aucun faux positif sur décor figé -----------------------
    for video in FALSE_REJECTIONS:
        reason = static_video_reason(video)
        if reason:
            failures.append(f"faux positif sur décor figé : {reason} || {video[:70]}")
    print(f"[1] faux positifs décor figé : {len(FALSE_REJECTIONS)} lignes testées")

    # --- 2. Le vrai défaut (pose statique) est rejeté ---------------------
    for video in TRUE_REJECTIONS:
        if not static_video_reason(video):
            failures.append(f"pose statique NON rejetée : {video[:70]}")
    print(f"[2] poses statiques rejetées : {len(TRUE_REJECTIONS)} lignes testées")

    # --- 3. Une ligne décorative sans verbe est acceptée (règle 6) -------
    for video in AMBIENT_OK:
        if static_video_reason(video):
            failures.append(f"ligne décorative rejetée à tort : {video[:70]}")
        if character_present(video):
            failures.append(f"character_present() faux positif : {video[:70]}")
    print(f"[3] lignes décoratives acceptées : {len(AMBIENT_OK)} lignes testées")

    # --- 4. Personnage détecté mais aucun verbe → rejet -------------------
    sans_verbe = "Video: A young woman in a red coat at a train platform."
    if not character_present(sans_verbe):
        failures.append("character_present() a raté une ligne avec personnage")
    if has_dynamic_verb(sans_verbe):
        failures.append("has_dynamic_verb() a détecté un verbe inexistant")
    if not static_video_reason(sans_verbe):
        failures.append("personnage sans verbe dynamique : NON rejeté")
    print("[4] personnage sans verbe : rejeté (attendu)")

    # --- 5. INVARIANT DE ROUTING : préfixe ASSET(plan N) -----------------
    statique = TRUE_REJECTIONS[0]
    err = _validator_static_error(statique)
    if err is None:
        failures.append("le validateur n'a pas rejeté la pose statique")
    else:
        if "ASSET(plan 1):" not in err:
            failures.append(f"préfixe ASSET(plan N) absent : {err[:120]}")
        if not is_asset_error(err):
            failures.append("is_asset_error() ne reconnaît plus l'erreur → routage cassé")
    # L'ancien bug : sans préfixe, is_asset_error() == False → AltSG.
    if is_asset_error("Value error, plan FIGÉ interdit dans Video"):
        failures.append("is_asset_error() matche une erreur sans préfixe ASSET(plan")
    print("[5] préfixe ASSET(plan N) + is_asset_error() : OK")

    # --- 6. Parité validateur / ScriptFixer ------------------------------
    for video in FALSE_REJECTIONS + TRUE_REJECTIONS + AMBIENT_OK:
        v_ok = _validator_static_error(video) is None
        f_ok = not any("plan FIGÉ" in e for e in _fixer_verdict(video))
        if v_ok != f_ok:
            failures.append(
                f"parité rompue (validateur={'OK' if v_ok else 'REJET'}, "
                f"fixer={'OK' if f_ok else 'REJET'}) : {video[:60]}"
            )
    print(f"[6] parité validateur/fixer : {len(FALSE_REJECTIONS + TRUE_REJECTIONS + AMBIENT_OK)} lignes")

    # --- 7. Verrou du mood musical ---------------------------------------
    v1 = "Audio: UPBEAT_GAMING\n\nPlan 1 (0-4s)\nVideo: A girl strides."
    v2 = "Audio: JRPG_BATTLE\n\nPlan 1 (0-4s)\nVideo: A girl spins."
    locked = _first_audio_value(v1)
    if locked != "UPBEAT_GAMING":
        failures.append(f"verrou audio mal lu : {locked}")
    forced, changed = _force_audio_value(v2, locked)
    if not changed or _first_audio_value(forced) != "UPBEAT_GAMING":
        failures.append(f"verrou audio non appliqué : {forced[:60]}")
    # Idempotent : réappliquer ne doit rien changer.
    again, changed2 = _force_audio_value(forced, locked)
    if changed2 or again != forced:
        failures.append("verrou audio non idempotent")
    # Pas d'Audio: dans le script → pas de verrou, pas de crash.
    if _first_audio_value("Plan 1 (0-4s)\nVideo: A girl strides.") is not None:
        failures.append("_first_audio_value() a inventé un Audio absent")
    if _force_audio_value("Plan 1 (0-4s)\nVideo: A girl strides.", "X")[1]:
        failures.append("_force_audio_value() a modifié un script sans Audio")
    print("[7] verrou du mood audio : OK")

    # --- Verdict ----------------------------------------------------------
    if failures:
        print(f"\nECHECS : {len(failures)}")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\nTOUS LES TESTS PASSENT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())