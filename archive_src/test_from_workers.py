import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from config import LLM_SCRIPTWRITER_ALT_MODEL
from nodes.scriptwriter.alt_script_generator import AltScriptGeneratorNode
from nodes.scriptwriter.script_reviewer import ScriptReviewerNode
from nodes.scriptwriter.pydantic_validation import GeneratedScriptAlt

ARTICLE = {
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
    "character": {"name": "Saber Alter", "franchise": "Fate/EXTRA Record"},
}

THINKING_AGENT = {
    "video_idea": "Saber Alter, l'antagoniste interdite de Fate, devient enfin jouable dans "
                  "Fate/EXTRA Record — et le fandom doit trancher : version pure ou déchue.",
    "why_it_works": "Première fois jouable d'une version corrompue + choix identitaire : le "
                    "spectateur doit choisir son camp, la waifu interdite enfin accessible.",
    "target_audience": "Fans de Fate et de waifus, joueurs PSP nostalgiques, communauté antibu.",
    "affiliate_angle": "candy.ai : créer et discuter avec sa compagne IA personnalisée.",
    "search_insights": "Le split pure/Alter est le visuel le plus partagé ; la polémique "
                       "'c'est la version traître ou pas' alimente les commentaires.",
    "visual_concept": "Split-screen Saber claire vs Saber Alter noire, même silhouette, "
                      "couleurs inversées.",
    "viral_mechanism": "hot_take",
    "spectator_stake": "Choisir son camp : Saber pure ou Saber Alter déchue.",
    "chosen_structure": "Sacrilege Comparison",
    "structure_why": "L'actu oppose la Saber Alter interdite à l'originale intouchable : "
                     "l'angle du sacrilège 'elle a remplacé la pure, choisis ton camp' "
                     "colle au débat du fandom.",
}

def check(cond: bool, label: str):
    icon = "✅" if cond else "❌"
    print(f"  {icon} {label}")
    if not cond:
        sys.exit(1)


async def main():
    import sys as _sys
    reuse = "--reuse" in _sys.argv
    print(f"Modèle SW InfoMissedGen : {LLM_SCRIPTWRITER_ALT_MODEL}")
    shared = {
        "topic": "Saber Alter enfin jouable dans Fate/EXTRA Record",
        "selected_article": ARTICLE,
        "thinking_agent": THINKING_AGENT,
        "pipeline_id": f"test-from-think-{int(time.time()*1000)}",
        "steps": [],
        "_traces": {},
    }
    _TRACES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces_from_workers.json")

    if reuse and os.path.isfile(_TRACES):
        prev = json.load(open(_TRACES))
        shared["script"] = prev.get("script", "")
        shared["_traces"] = prev.get("traces", {})
        print(f"\n========== REUSE script (skip SW InfoMissedGen) ==========")
    else:
        t0 = time.time()
        print("\n========== SW InfoMissedGen (structure choisie par le Thinking Agent) ==========")
        await AltScriptGeneratorNode().run_async(shared)
        print(f"  -> SW InfoMissedGen ({(time.time()-t0)/60:.0f}min)")
        with open(_TRACES, "w") as f:
            json.dump({
                "pipeline_id": shared.get("pipeline_id"),
                "ts": datetime.now(timezone.utc).isoformat(),
                "script": shared.get("script", ""),
                "traces": shared.get("_traces", {}),
            }, f, ensure_ascii=False, indent=2)
        print(f"  (script sauvegardé dans {_TRACES})")

    script = shared.get("script", "")
    check(bool(script.strip()), "Script non vide généré par SW InfoMissedGen")
    low = script.lower()
    check("candy" not in low and "download" not in low and "lien en bio" not in low
          and ".ai" not in low.replace("candy.", ""),
          "Aucune mention d'app/produit/lien (candy.ai) dans le script")

    t0 = time.time()
    print("\n========== SCRIPT REVIEWER (Kal relit) ==========")
    _err = None
    try:
        await ScriptReviewerNode().run_async(shared)
    except Exception as e:
        _err = e
    print(f"  -> stop ({(time.time()-t0)/60:.0f}min)")
    out = {
        "pipeline_id": shared.get("pipeline_id"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "script_before_review": script,
        "script_after_review": shared.get("script", ""),
        "reviewer_error": str(_err) if _err else None,
        "traces": shared.get("_traces", {}),
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces_from_workers.json")
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Sauvegardé (réponse LLM du reviewer incluse si crash): {path}")
    if _err:
        sys.exit(1)
    script = shared.get("script", "")
    print(script)

    if os.environ.get("RUN_PYDANTIC", "0") == "1":
        print("\n========== VALIDATION pydantic (GeneratedScriptAlt) ==========")
        try:
            GeneratedScriptAlt(script=script)
            print("  ✅ OK : structure, >= 7 plans, pas de filler, pas de texte dans Video:, "
                  "VO <= 12 mots, zéro VO redondante")
        except Exception as e:
            print(f"  ❌ ÉCHEC : {e}")
            sys.exit(1)
    else:
        print("\n(skip pydantic — RUN_PYDANTIC=1 pour activer)")


if __name__ == "__main__":
    asyncio.run(main())