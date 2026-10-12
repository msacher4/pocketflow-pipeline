#!/usr/bin/env python3
"""Test le LoRA VO via le proxy llama (:8080) avec la REGLE DE LONGUEUR.

Usage:
  python test_loravo_proxy.py            # sujet par défaut (Gojo, inédit du dataset)
  python test_loravo_proxy.py --no-rule  # sans la règle (montre la dérive ~17 mots)
"""
import json
import statistics
import sys
import urllib.request

PROXY = "http://localhost:8080/v1/completions"
MODEL = "Qwen3-14b-LoraVO"
LENGTH_RULE = "Chaque ligne fait 8 à 14 mots maximum. Lignes courtes, punchy, orales."
VO_RULE = "ne génère que les lignes VO, une par ligne. Pas de Video:/Plan:/Narration:/SFX:/Music:/Action:."
BANNED = ("Video:", "Plan:", "Narration:", "SFX:", "Music:", "Action:")

SUBJECT = {
    "sujet": "Jujutsu Kaisen Gojo Sealed",
    "mood": "EPIC_ORCHESTRAL",
    "perso": "Satoru Gojo",
    "franchise": "Jujutsu Kaisen",
    "hook": "The strongest sorcerer alive got sealed in a box because he let his guard down for one second",
    "idea": "Satoru Gojo, l'homme le plus fort du monde, s'est fait sceller dans un cube parce qu'il a baissé sa garde une seconde. Toi aussi tu perds tout le jour où tu te relâches.",
    "viral": "shock",
    "stake": "Rester vigilant même au sommet ou tout perdre en une seconde d'inattention.",
    "structure": "Fall From Grace",
}


def build_prompt(with_rule: bool) -> str:
    rule = f"{VO_RULE} {LENGTH_RULE}" if with_rule else f"{VO_RULE}"
    return (
        "Génère uniquement des lignes VO (Voice Over) pour un script short-form TikTok/Reels/YT Shorts.\n"
        f"Sujet: {SUBJECT['sujet']}\n"
        f"Humeur audio: {SUBJECT['mood']}\n"
        f"Personnage: {SUBJECT['perso']}\n"
        f"Franchise: {SUBJECT['franchise']}\n"
        f"Hook angle: {SUBJECT['hook']}\n"
        f"Video idea: {SUBJECT['idea']}\n"
        f"Viral mechanism: {SUBJECT['viral']}\n"
        f"Spectator stake: {SUBJECT['stake']}\n"
        f"Chosen structure: {SUBJECT['structure']}\n"
        f"Règle: {rule}\n\n### Response:\n"
    )


def main() -> None:
    with_rule = "--no-rule" not in sys.argv
    prompt = "### Instruction:\n" + build_prompt(with_rule)
    payload = json.dumps({
        "model": MODEL, "prompt": prompt, "max_tokens": 300,
        "temperature": 0.6, "top_p": 0.95, "top_k": 20,
    }).encode()
    req = urllib.request.Request(PROXY, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    text = d["choices"][0]["text"].strip()
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    words = [len(ln.split()) for ln in lines]
    bad = [ln for ln in lines if ln.startswith(BANNED)]
    print(f"--- règle longueur: {'OUI' if with_rule else 'NON'} ---")
    print(f"{len(lines)} lignes | mots: moy={statistics.mean(words):.1f} max={max(words)} | interdits: {len(bad)}")
    print("---")
    print(text)


if __name__ == "__main__":
    main()
