import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

import config
from config import LLM_SCRIPTWRITER_MODEL, LLM_SCRIPTWRITER_ALT_MODEL
from nodes.scriptwriter.thinking_agent import ThinkingAgentNode
from nodes.scriptwriter.alt_script_generator import AltScriptGeneratorNode
from nodes.scriptwriter.vo_coherence_review import VoCoherenceReviewNode
from nodes.scriptwriter.pydantic_validation import GeneratedScriptAlt


def check(cond: bool, label: str):
    icon = "✅" if cond else "❌"
    print(f"  {icon} {label}")
    if not cond:
        sys.exit(1)


def vos_from(script: str):
    return [l.strip()[4:] for l in script.splitlines() if l.strip().startswith("VO:")]


def check_structure(shared: dict, script: str):
    from helpers.call_llm import load_knowledge
    knowledge = load_knowledge("waifu_structures")
    samples = set()
    for line in knowledge.splitlines():
        l = line.strip()
        if l.startswith("|") and ("Sample VO" not in l and "|--" not in l):
            cells = [c.strip() for c in l.strip("|").split("|")]
            if len(cells) >= 4 and cells[3]:
                samples.add(cells[3].lower())
    vf = [v.lower() for v in vos_from(script)]
    copied = [v for v in vf if v in samples or any(s in v for s in samples)]
    check(not copied, f"aucune 'Sample VO' recopiée dans le script (recopiées: {copied or 'aucune'})")
    article = shared.get("selected_article", {})
    names = [n.lower() for n in [article.get("character", {}).get("name", ""),
                                 article.get("title", "")] if n]
    hook = vos_from(script)[0].lower() if vos_from(script) else ""
    check(any(n.split()[0] in hook for n in names) or "fate" in hook,
          f"le hook parle bien du sujet de l'actu (HOOK VO: {hook!r})")


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
            "character": {"name": "Saber Alter", "franchise": "Fate/EXTRA Record"},
        },
        "pipeline_id": f"test-full-alt-{int(time.time()*1000)}",
        "steps": [],
        "_traces": {},
    }


async def run_node(node, shared, label: str):
    t0 = time.time()
    print(f"\n========== {label} ({time.time():.0f}) ==========")
    action = await node.run_async(shared)
    dt = time.time() - t0
    print(f"  -> action={action} ({dt:.0f}s)")
    if shared.get("_error"):
        print(f"  ❌ _error={shared.get('_error')}")
        sys.exit(1)
    return action


async def main():
    print(f"Modèles : SW Thinking={LLM_SCRIPTWRITER_MODEL}, SW InfoMissedGen={LLM_SCRIPTWRITER_ALT_MODEL}")
    shared = base_shared()

    await run_node(ThinkingAgentNode(), shared, "THINKING AGENT (recherches web + angle viral + STRUCTURE)")

    print("\n========== THINKING AGENT — résultat ==========")
    print(json.dumps(shared.get("thinking_agent", {}), ensure_ascii=False, indent=2))
    chosen = shared.get("thinking_agent", {}).get("chosen_structure", "")
    check(bool(chosen.strip()), f"chosen_structure choisie (obtenu: {chosen!r})")

    await run_node(AltScriptGeneratorNode(), shared, "SW InfoMissedGen (suivant la structure choisie)")
    script = shared.get("script", "")
    print("\n========== SW InfoMissedGen — script final ==========")
    print(script)
    check_structure(shared, script)

    await run_node(VoCoherenceReviewNode(), shared, "VO COHERENCE REVIEW (cohérence/hallucination/lore)")
    script = shared.get("script", "")
    print("\n========== VO COHERENCE REVIEW — script relu ==========")
    print(script)

    print("\n========== VALIDATION pydantic (GeneratedScriptAlt) ==========")
    try:
        GeneratedScriptAlt(script=script)
        print("  ✅ Validation OK (structure, 7 plans, pas de filler, pas de texte dans "
              "Video:, VO <= 12 mots, zéro VO redondante)")
    except Exception as e:
        print(f"  ❌ Validation ÉCHOUÉE: {e}")
        sys.exit(1)

    print("\nDone! Chaîne complète ThinkingAgent → SW InfoMissedGen → VO Coherence Review validée.")
    print(f"Traces (LLM prompts/réponses) dans tests/traces_full_alt.json")

    out = {
        "pipeline_id": shared.get("pipeline_id"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "thinking_agent": shared.get("thinking_agent", {}),
        "chosen_structure": shared.get("thinking_agent", {}).get("chosen_structure", ""),
        "structure_why": shared.get("thinking_agent", {}).get("structure_why", ""),
        "script": shared.get("script", ""),
        "traces": shared.get("_traces", {}),
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces_full_alt.json")
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Sauvegardé : {path}")


if __name__ == "__main__":
    asyncio.run(main())