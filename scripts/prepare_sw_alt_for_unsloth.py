#!/usr/bin/env python3
"""Convertit datasets/sw_alt.jsonl -> format Unsloth/LlamaFactory (Option B).

- Exclut les exemples sans thinking_agent.
- instruction = brief contextuel (topic, mood, perso, hook, thinking_agent...).
- response = script = VO seules.
- Inclut la REGLE DE LONGUEUR (8-14 mots/ligne) prouvée le 2026-10-11 :
  sans elle le modèle dérive à ~17 mots/ligne (train = 14.5).
- Sorties : sw_alt_unsloth.jsonl (+ _train/_val, split 95/5 seed 42)
            + sw_alt_alpaca_train/val.json (format LlamaFactory).
"""
import json
import random
import sys
from pathlib import Path

BASE = Path("/media/marcs/Linux_Apps/Projets_AI/pocketflow_pipeline/datasets")
SRC = BASE / "sw_alt.jsonl"

LENGTH_RULE = "Chaque ligne fait 8 à 14 mots maximum. Lignes courtes, punchy, orales."
VO_RULE = "ne génère que les lignes VO, une par ligne. Pas de Video:/Plan:/Narration:/SFX:/Music:/Action:."


def build_instruction(d) -> str:
    ta = d["thinking_agent"]
    art = d["article"]
    return (
        "Génère uniquement des lignes VO (Voice Over) pour un script short-form TikTok/Reels/YT Shorts.\n"
        f"Sujet: {d['topic']}\n"
        f"Humeur audio: {d['audio_mood']}\n"
        f"Personnage: {art.get('character_name') or ''}\n"
        f"Franchise: {art.get('franchise') or ''}\n"
        f"Hook angle: {art.get('hook_angle') or ''}\n"
        f"Video idea: {ta.get('video_idea') or ''}\n"
        f"Viral mechanism: {ta.get('viral_mechanism') or ''}\n"
        f"Spectator stake: {ta.get('spectator_stake') or ''}\n"
        f"Chosen structure: {ta.get('chosen_structure') or ''}\n"
        f"Règle: {VO_RULE} {LENGTH_RULE}"
    )


def main() -> None:
    recs = []
    skipped = 0
    with open(SRC) as f:
        for line in f:
            d = json.loads(line)
            if d.get("thinking_agent") is None:
                skipped += 1
                continue
            recs.append({"instruction": build_instruction(d), "response": d["script"]})

    random.seed(42)
    random.shuffle(recs)
    n = len(recs)
    cut = int(0.95 * n)
    train, val = recs[:cut], recs[cut:]

    (BASE / "sw_alt_unsloth.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in recs) + "\n")
    (BASE / "sw_alt_unsloth_train.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in train) + "\n")
    (BASE / "sw_alt_unsloth_val.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in val) + "\n")
    (BASE / "sw_alt_alpaca_train.json").write_text(json.dumps(
        [{"instruction": x["instruction"], "input": "", "output": x["response"]} for x in train],
        ensure_ascii=False, indent=1))
    (BASE / "sw_alt_alpaca_val.json").write_text(json.dumps(
        [{"instruction": x["instruction"], "input": "", "output": x["response"]} for x in val],
        ensure_ascii=False, indent=1))
    print(f"OK: {n} exemples ({len(train)} train / {len(val)} val), {skipped} exclus (pas de thinking_agent)")


if __name__ == "__main__":
    sys.exit(main())
