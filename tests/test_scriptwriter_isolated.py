import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from nodes.scriptwriter import build_scriptwriter_flow


def check(cond: bool, label: str):
    icon = "✅" if cond else "❌"
    print(f"  {icon} {label}")
    if not cond:
        sys.exit(1)


def base_shared() -> dict:
    return {
        "topic": "motivation fitness",
        "selected_video": {
            "url": "https://www.youtube.com/watch?v=test",
            "description": "Vidéo de motivation sur la musculation et la discipline quotidienne.",
        },
        "video_analysis": {
            "summary": "Séquence de squats et pompes, rythme dynamique.",
            "duration_s": 45,
        },
        "pipeline_id": f"test-scriptwriter-{int(time.time()*1000)}",
        "steps": [],
        "_traces": {},
    }


async def scenario(label: str, shared: dict):
    print(f"\n=== SCÉNARIO: {label} ===")
    flow = build_scriptwriter_flow()
    await flow.run_async(shared)

    print("--- STEPS ---")
    for s in shared.get("steps", []):
        icon = {"ok": "✅", "error": "❌", "reformat": "🔁"}.get(s.get("status"), "⏳")
        print(f"  {icon} {s.get('step')}: {s.get('output', '')[:120]}")

    print("--- ASSERTIONS ---")
    check(not shared.get("_error"), f"pas d'erreur (error={shared.get('_error')})")

    script = shared.get("script", "")
    check(bool(script.strip()), f"script généré non vide ({len(script)} chars)")

    errors = [s for s in shared.get("steps", []) if s.get("status") == "error"]
    check(len(errors) == 0, f"aucune step en error (obtenu {len(errors)})")

    print(f"--- SCRIPT ({len(script)} chars) ---")
    print(script[:600])
    return script


async def test():
    await scenario("génération sans feedback", base_shared())

    shared2 = base_shared()
    shared2["script_feedback"] = "Plus de punch dans le Hook, moins de blabla dans le Body."
    script2 = await scenario("génération avec script_feedback", shared2)

    check(
        shared2.get("script_feedback", "") == "",
        "script_feedback effacé après injection",
    )

    if not script2:
        print("\n❌ Le script feedback n'a pas pu être généré, assertion finale ignorée.")
        sys.exit(1)

    print("\nDone! ScriptWriter natif validé (avec injection feedback).")


if __name__ == "__main__":
    asyncio.run(test())
