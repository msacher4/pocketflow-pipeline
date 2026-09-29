import asyncio
import logging
import os
import sys
import time

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
        "pipeline_id": f"dbg-chain-{int(time.time()*1000)}",
        "steps": [],
        "_traces": {},
    }


async def main():
    print(f"modèles: remote={LLM_SCRIPTWRITER_ALT_REMOTE_MODEL}, local={LLM_SCRIPTWRITER_ALT_MODEL}")
    shared = base_shared()

    t0 = time.time()
    await ThinkingAgentNode().run_async(shared)
    print(f"[1] thinking ({time.time()-t0:.0f}s)")

    for label, node in [
        ("SW InfoMissed", AltScriptGeneratorNode()),
        ("VO Coherence", VoCoherenceReviewNode()),
    ]:
        t0 = time.time()
        before = len(shared.get("script", ""))
        action = await node.run_async(shared)
        err = shared.get("_error")
        after = len(shared.get("script", ""))
        print(f"[{label}] action={action} ({time.time()-t0:.0f}s) len {before}->{after} err={err}")
        if err:
            print("ERROR:", err)
            break
        # dump input/output tail for verification
        script = shared.get("script", "")
        with open(f"/tmp/dbg_{label.replace(' ', '_')}.txt", "w") as f:
            f.write(script)


if __name__ == "__main__":
    asyncio.run(main())