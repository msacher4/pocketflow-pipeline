"""Rendu d'écoute : musique péchue/upbeat anime-JRPG via MusicGenerator (chemin alt).

Mini-run d'un seul slot audio taggé UPBEAT_GAMING -> acestep.cpp -> mp3 22s.
Permet de valider le nouveau profil `anime` à l'oreille avant les vrais runs.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(PIPELINE_ROOT / "nodes"))

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from pocketflow import AsyncFlow
from nodes.assetfinder.music_generator import MusicGenerator, _pick_profile

BLUEPRINT = {
    "audio": [
        {
            "id": "a1", "section": "hook", "position": 0, "type": "audio",
            "content": "UPBEAT_GAMING",
            "mood": "upbeat anime battle theme",
        },
    ],
}


async def main():
    profile = _pick_profile("upbeat anime battle theme", "UPBEAT_GAMING")
    print(">>> Profil choisi :", profile)

    shared = {
        "topic": "music render upbeat anime",
        "asset_blueprint": BLUEPRINT,
        "pipeline_id": f"test-music-upbeat-{int(time.time() * 1000)}",
        "steps": [],
        "_traces": {},
    }

    flow = AsyncFlow(start=MusicGenerator())
    await flow.run_async(shared)

    print("\n================ RESULTAT ================")
    for s in shared.get("steps", []):
        print(f"{s.get('step')}: {s.get('status')}")

    errors = [s for s in shared.get("steps", []) if s.get("status") != "ok"]
    if errors:
        print("ERREURS:", json.dumps(errors, ensure_ascii=False)[:2000])
        raise SystemExit(1)

    tracks = [a for a in shared.get("downloaded_audio", []) if a.get("type") == "music"]
    ok = False
    for t in tracks:
        p = Path(t["path"])
        ok = p.is_file() and p.stat().st_size > 0
        print(f"  {t.get('slot_id')} [{t.get('mood', '')}]: {t.get('path')} "
              f"({p.stat().st_size if p.is_file() else 0} o) {'OK' if ok else 'MANQUANT'}")
    print("\nECOUTE:", "OK" if ok else "ECHEC")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())