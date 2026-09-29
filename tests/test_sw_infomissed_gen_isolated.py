import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from config import LLM_SCRIPTWRITER_ALT_REMOTE_MODEL
from nodes.scriptwriter.alt_script_generator import AltScriptGeneratorNode


def check(cond: bool, label: str):
    icon = "✅" if cond else "❌"
    print(f"  {icon} {label}")
    if not cond:
        sys.exit(1)


def base_shared() -> dict:
    return {
        "topic": "Saber Alter enfin jouable dans Fate/EXTRA Record",
        "selected_article": {
            "title": "Fate/EXTRA Record dévoile Saber Alter comme Servant jouable",
            "summary": "Le remake de Fate/EXTRA confirme que Saber Alter sera une Servante jouable, "
                       "divisant le fandom entre ceux qui veulent la version déchue et ceux qui "
                       "réclament l'originale.",
            "synthesis": "Fate/EXTRA Record, le remake tant attendu du RPG PSP, ajoute Saber Alter "
                       "comme Servante jouable. C'est une première dans l'histoire jouable de la "
                       "franchise : la version corrompue de Saber a toujours été réservée aux "
                       "antagonistes. Le fandom est divisé sur ce choix.",
            "source": "Siliconera",
            "url": "https://www.siliconera.com/fate-extra-record-saber-alter-playable",
            "character": {"name": "Saber Alter", "franchise": "Fate/EXTRA"},
        },
        "thinking_agent": {
            "video_idea": "Saber Alter devient jouable pour la première fois — la version corrompue de l'héroïne la plus iconique de Fate est enfin aux mains des joueurs.",
            "why_it_works": "Le fandom attend ce remake depuis 15 ans et le choix de la version Alter divise sur le lore.",
            "target_audience": "Fans de Fate/EXTRA (18-35 ans) en attente du remake, joueurs de gacha anime",
            "affiliate_angle": "Candy.ai : River/Waifu-drama incite à créer et interagir avec la version Alter ou l'originale",
            "search_insights": "Le remake est très attendu, Saber Alter n'a jamais été jouable avant, le dualisme Saber/Alter est un sujet de débat récurrent du fandom.",
            "visual_concept": "Dualisme visuel Saber blonde lumineuse VS Saber Alter noire, la version Alter gagne du terrain à l'écran.",
            "viral_mechanism": "dilemma",
            "spectator_stake": "Le spectateur doit choisir son camp entre l'originale et la version déchue — et assumer son choix devant le fandom.",
            "chosen_structure": "The \"Final Boss Joins Your Side\"",
            "structure_why": "Saber Alter est une figure établie (2004, déjà jouable ailleurs) : l'actu est son passage jouable/mis en avant dans Fate/EXTRA Record. L'arc 'l'ennemi de toujours rejoint enfin ton camp' est lore-juste et colle au feeling badass du gameplay.",
        },
        "pipeline_id": f"test-alt-sg-{int(time.time()*1000)}",
        "steps": [],
        "_traces": {},
    }


async def scenario(label: str, shared: dict):
    print(f"\n=== SCÉNARIO: {label} ===")
    print(f"  modèle: {LLM_SCRIPTWRITER_ALT_REMOTE_MODEL} (via Go, GLM 5.3 Flash)")
    node = AltScriptGeneratorNode()
    await node.run_async(shared)

    print("--- ASSERTIONS ---")
    check(not shared.get("_error"), f"pas d'erreur (error={shared.get('_error')})")
    script = shared.get("script", "")
    check(bool(script.strip()), f"script généré non vide ({len(script)} chars)")

    print(f"--- SCRIPT ({len(script)} chars) ---")
    print(script[:2500])
    return script


async def test():
    shared = base_shared()
    script = await scenario("SW InfoMissed Gen isolé (GLM 5.3 Flash via Go)", shared)

    print("\n--- VÉRIFS FORMAT (rapides) ---")
    check("### HOOK" in script, "section HOOK présente")
    check("### BODY" in script, "section BODY présente")
    check("### CTA" in script, "section CTA présente")
    check("Audio:" in script, "ligne Audio présente")
    plan_count = sum(1 for l in script.splitlines() if l.strip().startswith("Plan "))
    check(plan_count >= 7, f"au moins 7 plans (obtenu {plan_count})")

    print(f"\nDone! SW InfoMissed Gen isolé validé — modèle {LLM_SCRIPTWRITER_ALT_REMOTE_MODEL}")
    return script


if __name__ == "__main__":
    asyncio.run(test())