import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from config import LLM_SCRIPTWRITER_MODEL, LLM_SCRIPTWRITER_ALT_MODEL, LLM_SCRIPTWRITER_ALT_REMOTE_MODEL
from nodes.scriptwriter.thinking_agent import ThinkingAgentNode
from nodes.scriptwriter.alt_script_generator import AltScriptGeneratorNode
from nodes.scriptwriter.vo_coherence_review import VoCoherenceReviewNode


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
                       "des fans depuis 2004, toujours en antagoniste jusqu'ici. Le fandom est "
                       "divisé sur ce choix.",
            "source": "Siliconera",
            "url": "https://www.siliconera.com/fate-extra-record-saber-alter-playable",
            "character": {"name": "Saber Alter", "franchise": "Fate/EXTRA Record"},
        },
        "pipeline_id": f"script-versions-{int(time.time()*1000)}",
        "steps": [],
        "_traces": {},
    }


def vos_from(script: str):
    return [l.strip()[3:].strip() for l in script.splitlines() if l.strip().startswith("VO:")]


async def run(node, shared, label: str, versions: dict):
    t0 = time.time()
    print(f"\n{'='*64}\n>>> {label}\n{'='*64}")
    action = await node.run_async(shared)
    dt = time.time() - t0
    print(f"  -> action={action} ({dt:.0f}s)\n")
    if shared.get("_error"):
        print(f"  ❌ _error={shared.get('_error')}")
        sys.exit(1)
    script = shared.get("script", "")
    versions[label] = script
    print(script)
    print()
    return script


async def main():
    print(f"Modèles : GLM remote={LLM_SCRIPTWRITER_ALT_REMOTE_MODEL}, "
          f"local={LLM_SCRIPTWRITER_ALT_MODEL}, thinking={LLM_SCRIPTWRITER_MODEL}")
    shared = base_shared()
    versions = {}

    # 1. Thinking (structure)
    t0 = time.time()
    print("\n" + "=" * 64 + "\n>>> THINKING AGENT\n" + "=" * 64)
    await ThinkingAgentNode().run_async(shared)
    print(f"  -> ({time.time()-t0:.0f}s) structure: "
          f"{shared.get('thinking_agent', {}).get('chosen_structure', '')!r}")

    # 2. SG + reviewers, version après chaque étape
    await run(AltScriptGeneratorNode(), shared,
              f"1. ScriptWriter (GLM 5.3 Flash remote)", versions)
    await run(VoCoherenceReviewNode(), shared,
              "2. VO Coherence Review — cohérence/hallucination/lore", versions)

    # Diff VO entre étapes
    print("\n" + "=" * 64 + "\nDIFFS VO ENTRE ÉTAPES\n" + "=" * 64)
    steps = list(versions)
    for a, b in zip(steps, steps[1:]):
        va, vb = vos_from(versions[a]), vos_from(versions[b])
        changed = [(i+1, va[i], vb[i]) for i in range(min(len(va), len(vb))) if va[i] != vb[i]]
        print(f"\n--- {steps.index(a)+1}. {a} -> {b} : {len(changed)} VO modifiée(s) ---")
        for i, before, after in changed:
            print(f"  Plan {i}:")
            print(f"    AVANT : {before}")
            print(f"    APRÈS : {after}")

    out = {
        "pipeline_id": shared.get("pipeline_id"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "thinking_agent": shared.get("thinking_agent", {}),
        "versions": versions,
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "script_versions_alt.json")
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nSauvegardé : {path}")


if __name__ == "__main__":
    asyncio.run(main())
