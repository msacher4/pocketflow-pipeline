"""A/B test expressivité Fish Speech S2 (profil woman_news) — sans toucher à la prod.

Génère 3 variantes par ligne VO dans output/voice_ab_test/ :
  A = baseline        (temp 0.7, top_p 0.7, top_k 30)                pas de pitch shift
  B = expressif       (temp 0.9, top_p 0.9, top_k 40)                pas de pitch shift
  C = expressif+grave (temp 0.9, top_p 0.9, top_k 40)                pitch -1 demi-ton

Usage :
  python tests/voice_ab_test.py            # tout générer
  python tests/voice_ab_test.py B          # ne générer que la variante B
"""

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from helpers.comfyui_api import s2_generate_voice, S2_VOICE_PROFILES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice-ab-test")

OUT_DIR = PIPELINE_ROOT / "output" / "voice_ab_test"

# Texte VO exact des sous-titres du run 20260908_093702
VO_LINES = {
    "v1": "Hatsune Miku just teamed up with Honkai Star.",
    "v2": "Their new medley blends two iconic anime soundtracks.",
    "v3": "Fans are calling this the ultimate crossover event.",
    "v4": "Watch the full video and comment favorite song.",
}

BASELINE = {"temperature": 0.7, "top_p": 0.7, "top_k": 30, "max_new_tokens": 1024}
EXPRESSIF = {"temperature": 0.9, "top_p": 0.9, "top_k": 40, "max_new_tokens": 1024}

# -1 demi-ton => facteur 2^(-1/12)
SEMITONE_DOWN = 2 ** (-1 / 12)


def pitch_shift_down(src: Path, dst: Path, factor: float) -> Path:
    """Abaisse la hauteur de `factor` (0<factor<1) en préservant la durée (ffmpeg)."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    )
    try:
        sr = int(probe.stdout.strip())
    except ValueError:
        sr = 44100
    new_rate = int(round(sr * factor))
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-af", f"asetrate={new_rate},aresample={sr},atempo=1/{factor:.6f}",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dst.is_file():
        raise RuntimeError(f"pitch shift failed: {r.stderr[:500]}")
    return dst


async def gen_variant(text: str, dest_dir: Path, params: dict) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    return await s2_generate_voice(
        text=text,
        dest_dir=dest_dir,
        name="vo",
        voice="woman_news",
        voice_dir=S2_VOICE_PROFILES,
        temperature=params["temperature"],
        top_p=params["top_p"],
        top_k=params["top_k"],
        max_new_tokens=params["max_new_tokens"],
    )


async def main():
    only = sys.argv[1].upper() if len(sys.argv) > 1 else None
    variants = {"A": BASELINE, "B": EXPRESSIF, "C": EXPRESSIF}
    if only:
        if only not in variants:
            print(f"Variante inconnue: {only} (choisir A, B ou C)")
            raise SystemExit(2)
        variants = {only: variants[only]}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for vname, params in variants.items():
        for vo_id, text in VO_LINES.items():
            sub = OUT_DIR / f"{vname}_{vo_id}"
            raw = await gen_variant(text, sub, params)
            if not raw:
                log.error(f"{vname}_{vo_id}: échec génération")
                continue
            src = Path(raw)
            if vname == "C":
                final = sub / f"vo_pitchdown.wav"
                pitch_shift_down(src, final, SEMITONE_DOWN)
            else:
                final = src  # vo.wav
            size = final.stat().st_size if final.is_file() else 0
            print(f"  {vname}_{vo_id:4s} -> {final} ({size} o)")
            count += 1

    print(f"\nTerminé: {count} fichier(s) dans {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
