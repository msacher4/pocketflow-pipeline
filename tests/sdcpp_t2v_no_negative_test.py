"""Test isolé T2V : impact du négatif codé en dur sur les plans 5, 6 et 7.

Contexte — run 06/10 : les plans 3 et 4 sont sortis correctement, les plans 5,
6 et 7 en slop. Les trois plans slop sont les seuls plans d'INTÉRIEUR STATIF,
et AssetPlanner injectait à chaque slot le même négatif forcé :

    "blurry, low quality, text, watermark, deformed, indoor apartment, selfie,
     casual home video, static room"

soit au modèle « surtout pas une pièce, surtout pas une image figée » — exactement
ce que décrivent les prompts 5, 6 et 7. Le plan 5 dit même littéralement
« a dim office », le plan 7 « a cluttered gaming desk ».

Ce test fait un A/B sur CHAQUE plan, même seed des deux côtés pour isoler la
seule variable :

    <plan>_old    négatif codé en dur (reproduction de la prod d'avant-fix)
    <plan>_noneg  négatif = None -> sdcpp_api retombe sur
                  SDCPP_DEFAULT_NEGATIVE ("worst quality, low quality, blurry,
                  distorted, artifacts") qui ne contient NI « indoor apartment »
                  NI « static room ».

Seul le négatif change : prompt, seed, frames, cfg, guidance et passe de hires
sont ceux de la prod (helpers/sdcpp_api._build_cmd, appelé tel quel).

Usage :
    python3 tests/sdcpp_t2v_no_negative_test.py            # 5, 6, 7 (6 clips)
    python3 tests/sdcpp_t2v_no_negative_test.py 5           # plan 5 seul (2 clips)
    python3 tests/sdcpp_t2v_no_negative_test.py 5 old       # variante seule

Durée : ~6 min par clip -> 5+6+7 complet ≈ 36 min.
Sortie : downloads/negtest_<date>/plan{N}_{old|noneg}.webm
Verdict : à l'œil — le plan 5_noneg a-t-il repris une pièce propre avec un
violon lisible, et le 7_noneg un bureau lisible, contre les sorties _old ?
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from helpers.sdcpp_api import (  # noqa: E402
    SDCPP_DEFAULT_NEGATIVE,
    sdcpp_generate_video_t2v,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("pocketflow-pipeline").setLevel(logging.WARNING)
log = logging.getLogger("negtest")

OUT = PIPELINE_ROOT / "downloads" / f"negtest_{datetime.now():%Y%m%d_%H%M%S}"

NEG_OLD = (
    "blurry, low quality, text, watermark, deformed, "
    "indoor apartment, selfie, casual home video, static room"
)

PROMPTS = {
    5: (
        "A vintage violin lies alone on papers in a dim office, bow sliding "
        "across strings under a warm desk lamp, slow overhead zoom, haunting "
        "tone and clock ticking, amber light against cold shadows"
    ),
    6: (
        "Two glowing arcade cabinets sit side by side, one red and one electric "
        "blue, sparks bouncing off reflective floor, slow push-in, arcade hum "
        "and rising crowd roar, dual red-blue lighting"
    ),
    7: (
        "A smartphone lies alone on a cluttered gaming desk, blank screen "
        "glowing, controller and drink cans around, slow overhead zoom, "
        "keyboard clicks and notification chime, warm lamp with cool monitor glow"
    ),
}

SEEDS = {5: 50005, 6: 50006, 7: 50007}
VARIANTS = ("old", "noneg")


def _negative_for(variant: str) -> str | None:
    if variant == "noneg":
        return None
    return f"{NEG_OLD}, {SDCPP_DEFAULT_NEGATIVE}"


async def _run(plan: int, variant: str) -> str | None:
    prompt = PROMPTS[plan]
    neg = _negative_for(variant)
    name = f"plan{plan}_{variant}"
    out = OUT / f"{name}.webm"

    (OUT / f"{name}.txt").write_text(
        f"plan={plan} variant={variant} seed={SEEDS[plan]}\n"
        f"prompt={prompt}\n"
        f"negative={neg}\n"
    )
    log.info(f"[{name}] seed={SEEDS[plan]} negative={'(None)' if neg is None else neg}")
    log.info(f"[{name}] prompt={prompt[:100]}...")

    path = await sdcpp_generate_video_t2v(
        prompt=prompt,
        dest_dir=OUT,
        name=name,
        seed=SEEDS[plan],
        negative=neg,
    )
    if not path:
        log.error(f"[{name}] ECHEC")
        return None
    size = Path(path).stat().st_size
    log.info(f"[{name}] OK {size} bytes -> {path}")
    return path


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    plans = [int(a) for a in args if a.isdigit() and 5 <= int(a) <= 7] or [5, 6, 7]
    variants = [a for a in args if a in VARIANTS] or list(VARIANTS)

    OUT.mkdir(parents=True, exist_ok=True)
    log.info(f"output = {OUT}")
    log.info(f"plans = {plans} | variants = {variants}")

    ok = 0
    total = len(plans) * len(variants)
    for plan in plans:
        for variant in variants:
            if await _run(plan, variant):
                ok += 1

    print()
    print("=" * 60)
    print(f"DONE {ok}/{total} clips -> {OUT}")
    for p in plans:
        for v in variants:
            f = OUT / f"plan{p}_{v}.webm"
            print(f"  {'OK ' if f.is_file() else 'FAIL'} plan{p}_{v}")
    print("=" * 60)
    raise SystemExit(0 if ok == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
