"""Test unitaire du choix du profil musical (MusicGenerator).

Chemin alt (jeux vidéo/anime) : tag Audio: upbeat ou mood péchu -> profil anime/videogame.
Chemin classique : vibes sombres/club préservées (phonk, deephouse, melodictrap).
Aucun modèle, aucun GPU — pure logique de sélection.
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(PIPELINE_ROOT / "nodes"))

from nodes.assetfinder.music_generator import MUSIC_PROFILES, _pick_profile

CASES = [
    # (tag Audio:, mood, attendu)
    ("UPBEAT_GAMING", "", "anime"),
    ("UPBEAT GAMING", "", "anime"),
    ("ANIME_OPENING", "", "anime"),
    ("ANIME_BATTLE", "", "anime"),
    ("JRPG_BATTLE", "", "anime"),
    ("JRPG", "", "anime"),
    ("VIDEOGAME", "", "videogame"),
    ("CHIPTUNE", "", "videogame"),
    ("ARCADE", "", "videogame"),
    ("", "upbeat anime battle theme", "anime"),
    ("", "heroic video-game drop", "anime"),
    ("", "peppy JRPG opening", "anime"),
    ("", "energetic shonen opening", "anime"),
    # Chemin classique : genres sombres/club préservés
    ("DARK_GOTHIC_ORCHESTRAL", "", "melodictrap"),
    ("MUSIQUE_ENTRAINANTE", "", "melodictrap"),
    ("", "dark aggressive trap", "phonk"),
    ("", "deep club house groove", "deephouse"),
]


def main():
    failures = 0
    for tag, mood, expected in CASES:
        got = _pick_profile(mood, tag)
        want = MUSIC_PROFILES[expected]
        ok = got == want
        name = next((k for k, v in MUSIC_PROFILES.items() if v == got), "?")
        print(f"  tag={tag!r:24} mood={mood!r:38} -> {name:12} {'OK' if ok else 'KO (attendu ' + expected + ')'}")
        failures += 0 if ok else 1
    print("\nRESULTAT:", "OK" if failures == 0 else f"{failures} ECHEC(S)")
    raise SystemExit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()