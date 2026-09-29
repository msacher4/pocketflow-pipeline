import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from config import LLM_SCRIPTWRITER_ALT_MODEL
from nodes.scriptwriter.vo_coherence_review import VoCoherenceReviewNode


def check(cond: bool, label: str):
    icon = "✅" if cond else "❌"
    print(f"  {icon} {label}")
    if not cond:
        sys.exit(1)


# Script d'entrée avec 5 VO piégées (4 axes du soul) :
#  - P1 (hook)   : temps verbal cassé ("just flip her")                    [axe 1]
#  - P3          : redondance avec P2 (titre du jeu répété) + calque
#                  "make her whole moveset from zero"                    [axes 4+2]
#  - P4          : expression hallucinée type Tradfoire ("photocopies")   [axe 2]
#  - P5          : lore faux "never existed until now" (Alter = 2004)    [axe 3b]
#  - P6          : nom d'arme pris chez un AUTRE personnage Fate
#                  ("Excalibur Muramasa" — la lame d'Alter n'a pas ce nom) [axe 3a]
# Les autres VOs sont propres : elles doivent rester identiques au mot près.
SCRIPT_BUGGY = """Audio: UPBEAT_GAMING

### HOOK (0-3s)
Plan 1 (0-3s)
Video: Saber Alter in her black armor, red glowing eyes flaring, camera dolly-in
VO: The most feared heroine just flip her allegiance after twenty years.

-- Transition --
SFX: Whoosh

### BODY (3-22s)
Plan 2 (3-6s)
Video: Saber Alter lowering her corrupted sword, dark particles around her, tracking shot
VO: Fate/EXTRA Record finally puts her on your side.

Plan 3 (6-10s)
Video: A dark armored swordswoman unleashing a brutal slash, dust shockwave, low angle
VO: Fate/EXTRA Record gives it to you, they make her whole moveset from zero, brutal.

Plan 4 (10-14s)
Video: A black-armored swordswoman striking the ground, embers erupting, handheld camera
VO: The boss of your a thousand photocopies of runs is now your weapon.

Plan 5 (14-18s)
Video: A corrupted knight bursting through a gate, red light cracking the sky, push-in
VO: This corrupted version never existed until now.

Plan 6 (18-22s)
Video: A dark swordswoman raising her black blade, black energy erupting from its tip, upward tilt
VO: Her black blade Excalibur Muramasa lights up the whole screen.

### CTA (22-26s)
Plan 7 (22-26s)
Video: A golden knight and a black knight charging toward camera, split-screen clash
VO: Radiant or corrupted? Comment your camp.
"""


def base_shared() -> dict:
    return {
        "topic": "Saber Alter enfin jouable dans Fate/EXTRA Record",
        "selected_article": {
            "title": "Fate/EXTRA Record dévoile Saber Alter comme Servant jouable",
            "summary": "Le remake de Fate/EXTRA confirme que Saber Alter sera une Servante jouable, "
                       "divisant le fandom entre ceux qui veulent la version déchue et ceux qui "
                       "réclament l'originale.",
            "synthesis": "Fate/EXTRA Record, le remake tant attendu du RPG PSP, ajoute Saber Alter "
                       "comme Servante jouable. Saber Alter est la version corrompue de Saber, connue "
                       "des fans depuis les années 2000, toujours en antagoniste jusqu'ici. Le fandom "
                       "est divisé sur ce choix.",
            "source": "Siliconera",
            "url": "https://www.siliconera.com/fate-extra-record-saber-alter-playable",
            "character": {"name": "Saber Alter", "franchise": "Fate/EXTRA"},
        },
        "thinking_agent": {
            "chosen_structure": "The \"Final Boss Joins Your Side\"",
            "structure_why": "Saber Alter est une figure établie : l'arc 'l'ennemi de toujours rejoint ton camp' est lore-juste.",
            "viral_mechanism": "dilemma",
            "spectator_stake": "Choisir son camp entre l'originale et la version déchue.",
        },
        "script": SCRIPT_BUGGY,
        "pipeline_id": f"test-vo-review-{int(time.time()*1000)}",
        "steps": [],
        "_traces": {},
    }


def extract_vos(script: str) -> list:
    return [l.strip() for l in script.splitlines()
            if l.strip().startswith("VO:")]


if __name__ == "__main__":
    async def test():
        print(f"\n=== SCÉNARIO: VO Coherence Review isolé (qwen3.8-27b) ===")
        print(f"  modèle: {LLM_SCRIPTWRITER_ALT_MODEL}")
        shared = base_shared()
        node = VoCoherenceReviewNode()
        await node.run_async(shared)

        print("--- ASSERTIONS ---")
        check(not shared.get("_error"), f"pas d'erreur (error={shared.get('_error')})")
        fixed = shared.get("script", "")
        check(bool(fixed.strip()), f"script relu non vide ({len(fixed)} chars)")
        print(f"\n--- SCRIPT CORRIGÉ ({len(fixed)} chars) ---")
        print(fixed)

        vos_before = extract_vos(SCRIPT_BUGGY)
        vos_after = extract_vos(fixed)
        check(len(vos_before) == len(vos_after), f"nombre de VOs inchangé ({len(vos_before)})")

        # AUDIT obligatoire : une entrée par Plan
        audit = shared.get("_vo_audit", [])
        audited_plans = {e.get("plan") for e in audit if isinstance(e, dict)}
        check(audited_plans == {1, 2, 3, 4, 5, 6, 7},
              f"audit couvre les 7 plans ({sorted(audited_plans)})")
        corrected = [e.get("plan") for e in audit
                     if isinstance(e, dict) and e.get("verdict") != "OK"]
        print(f"  plans corrigés selon l'audit: {corrected}")
        expect_faulty = {1, 3, 4, 5, 6}
        check(expect_faulty.issubset(set(corrected)),
              f"audit accuse bien les 5 VOs fautives 1,3,4,5,6 (accusées: {corrected})")

        # Structure strictement préservée
        check("### HOOK" in fixed or "HOOK" in fixed.upper(), "section HOOK préservée")
        check("### BODY" in fixed.lower() or "BODY" in fixed.upper(), "section BODY préservée")
        check("### CTA" in fixed.upper() or "CTA" in fixed.upper(), "section CTA préservée")
        check(sum(1 for l in fixed.splitlines() if l.strip().startswith("Plan ")) == 7,
              "7 plans préservés")

        # Vidéos untouched (les lignes Video: restent identiques)
        videos_before = [l.strip() for l in SCRIPT_BUGGY.splitlines()
                         if l.strip().startswith("Video:")]
        videos_after = [l.strip() for l in fixed.splitlines()
                        if l.strip().startswith("Video:")]
        check(videos_before == videos_after, "lignes Video: 100 % inchangées")

        print("\n--- VO saines (doivent rester quasi identiques) ---")
        # VOs propres d'entrée : index 1 (P2), 6 (CTA) —
        # les indices 0,2,3,4,5 sont fautifs dans SCRIPT_BUGGY.
        # Critère : recouvrement lexical >= 70 % (tolère une retouche cosmétique,
        # détecte une réécriture profonde).
        def overlap(a: str, b: str) -> float:
            wa = {w.strip(".,!?").lower() for w in a.split() if w.strip()}
            wb = {w.strip(".,!?").lower() for w in b.split() if w.strip()}
            if not wb:
                return 0.0
            return len(wa & wb) / len(wb)
        for idx in (1, 6):
            if idx < len(vos_after):
                ov = overlap(vos_after[idx], vos_before[idx])
                check(ov >= 0.7,
                      f"VO {idx+1} préservée ({ov:.0%}): {vos_after[idx][:60]}")

        print("\n--- VOs fautives (marqueurs d'erreur disparus) ---")
        # P1 : "just flip her" = temps+accord cassés → base verbale nue "flip her"
        # prohibée ("flipped her" elle-même ne contient pas ce substring)
        check("flip her" not in vos_after[0].lower(),
              f"P1 corrigée: {vos_after[0][:80]}")
        # P3 : "make her whole moveset" = calque → doit disparaître
        check("make her whole moveset" not in vos_after[2].lower(),
              f"P3 corrigée: {vos_after[2][:80]}")
        # P4 : 'photocopies' = expression hallucinée → doit disparaître
        check("photocopies" not in vos_after[3].lower(),
              f"P4 corrigée: {vos_after[3][:80]}")
        # P5 : 'never existed' = lore faux → doit disparaître
        check("never existed" not in vos_after[4].lower(),
              f"P5 corrigée: {vos_after[4][:80]}")
        # P6 : 'muramasa' = nom d'arme d'un AUTRE perso Fate → doit disparaître
        # DE TOUT LE SCRIPT (VO + éventuel résidu ailleurs)
        check("muramasa" not in fixed.lower(),
              f"P6 corrigée (zéro Muramasa dans le script entier): {vos_after[5][:80]}")
        # P3 : redondance — P2 et P3 ne doivent plus citer tous les deux le titre
        n_title = sum(1 for v in vos_after if "fate/extra record" in v.lower())
        check(n_title <= 1,
              f"redondance titre du jeu cassée ({n_title} VOs citent encore le titre)")

        # Axe 5 : le nom du perso doit apparaître dans la VO du Plan 1 OU 2
        name = shared["selected_article"]["character"]["name"]
        early_vos = " || ".join(vos_after[:2])
        check(name.lower() in early_vos.lower(),
              f"personnage nommé dans Plan 1 ou 2: {vos_after[0][:60]} / {vos_after[1][:60]}")

        print(f"\nDone! VO Coherence Review isolé validé — modèle {LLM_SCRIPTWRITER_ALT_MODEL}")

    asyncio.run(test())
