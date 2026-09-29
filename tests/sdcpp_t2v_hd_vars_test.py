"""Phase 1b — isoler « résolution » vs « durée » sur le verdict HD.

Phase 1 a échoué : en 352x640x33f les 4 seeds sont conformes (HD n'en garde que 1/6).
« HD » mélange 2 variables — ce script les sépare :

  A : 704x1280 x 33f  -> résolution HD, durée courte   (teste H1 : la résolution tue)
  B : 352x640  x 65f  -> résolution basse, durée HD    (teste H2 : la durée tue)

Sur les 2 seeds extremes (prompt clip_3 + négatif prod, identiques au HD) :

  seed 1234567    -> verdict HD ✅ OK
  seed 3733320982 -> verdict HD ❌ SLOP (= clip_3 du run prod)

Contraintes LTX : dims /32 (704,1280,352,640 ✓), frames ≡ 1 mod 8 (33, 65 ✓).

Lecture des résultats :
  A2 SLOP              -> H1 confirmée : la résolution tue la cohérence.
  A2 OK + B2 SLOP      -> H2 confirmée : la durée tue.
  A2 OK + B2 OK        -> interaction, 1 test combo nécessaire (544x960x65).

Usage : python3 tests/sdcpp_t2v_hd_vars_test.py [A1|A2|B1|B2|all]   (~3-6 min/cas)
Sortie : downloads/hd_vars/*.webm + *.log
"""

import asyncio
import logging
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from config import (
    SDCPP_BIN, SDCPP_ROCM_LIB, SDCPP_LTX25_DIFFUSION, SDCPP_LTX25_VAE,
    SDCPP_LTX25_LLM, SDCPP_TIMEOUT,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hd-vars-test")

OUT = PIPELINE_ROOT / "downloads" / "hd_vars"
OUT.mkdir(parents=True, exist_ok=True)

PROMPT_CLIP3 = (
    "A dark armored swordswoman sprints alone through a shattered digital arena, "
    "her heavy black cape whipping behind her, debris flying with each stride, "
    "a slow tracking shot keeps her centered, metal heeled boots striking the "
    "ground, stark violet rays of light cutting through dust"
)

NEG_PROD = (
    "blurry, low quality, text, watermark, deformed, indoor apartment, selfie, "
    "casual home video, static room, worst quality, low quality, blurry, "
    "distorted, artifacts, second person, two people, extra people, crowd"
)

SEED_OK = 1234567
SEED_SLOP = 3733320982

# case -> (width, height, frames, seed, expect HD)
CASES = {
    "C": (352, 640, 33, SEED_OK, "OK"),  # contrôle = params screen_test qui passaient à 11h50
    "A1": (704, 1280, 33, SEED_OK, "OK"),
    "A2": (704, 1280, 33, SEED_SLOP, "SLOP"),
    "B1": (352, 640, 65, SEED_OK, "OK"),
    "B2": (352, 640, 65, SEED_SLOP, "SLOP"),
    # Phase 1c : résolution "un tout petit peu" inférieure, 65 frames conservés
    "D": (672, 1216, 65, SEED_SLOP, "SLOP"),
    "D2": (672, 1216, 65, SEED_OK, "OK"),
    "E": (640, 1152, 65, SEED_SLOP, "SLOP"),
    # Panel 1c (672x1216x65) : les 2 autres seeds maudites HD + contrôle
    "F42": (672, 1216, 65, 42, "SLOP"),
    "F987": (672, 1216, 65, 987654321, "SLOP"),
    # Phase 1d — bissecte du seuil (65 frames), Round 1 : L2 = 608x1088
    "G1": (608, 1088, 65, 1234567, "?"),
    "G2": (608, 1088, 65, 3733320982, "?"),
    "G3": (608, 1088, 65, 42, "?"),
    "G4": (608, 1088, 65, 987654321, "?"),
    "G5": (608, 1088, 65, 5555555, "?"),
    # Phase 1d — Round 2 : L2 slop (4/5) → descente L1 = 576x1024 (9:16 exact)
    "H1": (576, 1024, 65, 1234567, "?"),
    "H2": (576, 1024, 65, 3733320982, "?"),
    "H3": (576, 1024, 65, 42, "?"),
    # Phase 1d — Round 3 : L1 slop (3/3) → descente L0 = 544x960 (34M)
    "J1": (544, 960, 65, 1234567, "?"),
    "J2": (544, 960, 65, 3733320982, "?"),
    "J3": (544, 960, 65, 42, "?"),
    # Phase 1d — Round 4 : L0 slop (3/3) → bissecte milieu (14.6M,34M) = 448x832 (24.2M)
    "K1": (448, 832, 65, 1234567, "?"),
    "K2": (448, 832, 65, 3733320982, "?"),
    "K3": (448, 832, 65, 42, "?"),
    # Phase 1d — Round 5 : K slop (2/3) → bissecte (352x640 propre, 448x832 slop) = 416x768
    "L1": (416, 768, 65, 1234567, "?"),
    "L2": (416, 768, 65, 3733320982, "?"),
    "L3": (416, 768, 65, 42, "?"),
    # Phase 1d — Round 6 : L slop (1/3 suffit) → bissecte = 384x704 (17.6M)
    "M1": (384, 704, 65, 1234567, "?"),
    "M2": (384, 704, 65, 3733320982, "?"),
    "M3": (384, 704, 65, 42, "?"),
    # Phase 2 — hypothèse "spec non respectée" : meme forme que J1-J3 (544x960, 3/3
    # slop a 65f/16fps) mais a 97f/24fps (spec LTX : fps 24/25/48/50, 97 = 8k+1).
    # Si le slop disparait, la cause etait le fps/duree et pas la resolution.
    "N1": (544, 960, 97, 1234567, "?"),
    "N2": (544, 960, 97, 3733320982, "?"),
    "N3": (544, 960, 97, 42, "?"),
    # Phase 7 — N1/N2/N3 a l'identique (544x960 / 97f / 24fps / cfg 3.0 /
    # euler / sigmas defaut / steps 8), SEULE difference : pas de negative
    # prompt. Compare V vs N = effet isole du negative prompt a cfg 3.0.
    # Reference N = 1/3 (N1 slop, N2 OK, N3 slop).
    "V1": (544, 960, 97, 1234567, "?"),
    "V2": (544, 960, 97, 3733320982, "?"),
    "V3": (544, 960, 97, 42, "?"),
    # Phase 3 — memes formes que N (544x960 / 97f / 24fps) mais avec les
    # reglages officiels LTX-2.5 distilled : CFG 1.0, PAS de negative
    # (positive-only), guidance 3.5 (spec "3-3.5"), sampler res_2s (spec
    # distilled). Compare P vs N = effet isole des reglages.
    "P1": (544, 960, 97, 1234567, "?"),
    "P2": (544, 960, 97, 3733320982, "?"),
    "P3": (544, 960, 97, 42, "?"),
    # Phase 4 — VRAIE spec LTX-2.5 distilled (corrige P qui avait res_2s +
    # guidance 3.5) : sampler euler_a (ANCESTRAL_SAMPLER_SINCE_VERSION=(2,5)),
    # sigmas officiels DISTILLED_SIGMA_VALUES, guidance 0 (aucun guider),
    # cfg 1.0, aucune negative. Meme forme que N/P pour comparaison isolee.
    "Q1": (544, 960, 97, 1234567, "?"),
    "Q2": (544, 960, 97, 3733320982, "?"),
    "Q3": (544, 960, 97, 42, "?"),
    # Phase 5 — hypothese "budget de tokens" : tot = latents x (L/32 x H/32).
    # Palier observe entre 2808 tokens (416x768x65, propre) et 3276 (448x832x65,
    # slop). Q1 (6630) slop. Test discriminant : meme nombre de latents (13)
    # mais 2860 tokens -> theoriquement propre si c'est bien le budget.
    "R1": (352, 640, 97, 1234567, "?"),
    "R2": (352, 640, 97, 3733320982, "?"),
    "R3": (352, 640, 97, 42, "?"),
    # Controle meme latents, tokens au-dessus du palier (4056) -> slop attendu
    "S1": (416, 768, 97, 1234567, "?"),
    # Phase 6 — desambiguation : frames et fps etaient confondus (65f TOUJOURS
    # a 16fps = propre, 97f TOUJOURS a 24fps = slop). Trois cellules pour
    # isoler la vraie variable.
    # T1 : 65f/16fps/4.06s — seed 1234567 prouvable propre ici (L1)
    "T1": (416, 768, 65, 1234567, "?"),
    # T2 : 65f/24fps/2.70s — meme latent que T1 mais fps spec
    "T2": (416, 768, 65, 1234567, "?"),
    # T3 : 97f/16fps/6.06s — meme latents que le lot slop, fps du lot propre
    "T3": (416, 768, 97, 1234567, "?"),
}

# Cases qui passent --fps explicitement (les autres laissent sd-cli a 16).
FPS_CASES = {"N1": 24, "N2": 24, "N3": 24,
             "V1": 24, "V2": 24, "V3": 24,
             "P1": 24, "P2": 24, "P3": 24,
             "Q1": 24, "Q2": 24, "Q3": 24,
             "R1": 24, "R2": 24, "R3": 24, "S1": 24}

# DISTILLED_SIGMA_VALUES depuis ltx_pipelines/utils/constants.py (LTX-2.5 distilled)
OFFICIAL_DISTILLED_SIGMAS = ("1.0,0.99375,0.9875,0.98125,0.975,"
                            "0.909375,0.725,0.421875,0.0")

# case -> (cfg, negative, guidance, sampler, sigmas)
SPEC_CASES = {
    "P1": (1.0, None, 3.5, "res_2s", None),
    "P2": (1.0, None, 3.5, "res_2s", None),
    "P3": (1.0, None, 3.5, "res_2s", None),
    "Q1": (1.0, None, 0.0, "euler_a", OFFICIAL_DISTILLED_SIGMAS),
    "Q2": (1.0, None, 0.0, "euler_a", OFFICIAL_DISTILLED_SIGMAS),
    "Q3": (1.0, None, 0.0, "euler_a", OFFICIAL_DISTILLED_SIGMAS),
    "R1": (1.0, None, 0.0, "euler_a", OFFICIAL_DISTILLED_SIGMAS),
    "R2": (1.0, None, 0.0, "euler_a", OFFICIAL_DISTILLED_SIGMAS),
    "R3": (1.0, None, 0.0, "euler_a", OFFICIAL_DISTILLED_SIGMAS),
    "S1": (1.0, None, 0.0, "euler_a", OFFICIAL_DISTILLED_SIGMAS),
    # Phase 7 : N a l'identique sauf negative=None -> pas de -n
    "V1": (3.0, None, 3.5, "euler", None),
    "V2": (3.0, None, 3.5, "euler", None),
    "V3": (3.0, None, 3.5, "euler", None),
}


def _env() -> dict:
    import os
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = SDCPP_ROCM_LIB
    env["GGML_CUDA_DISABLE_GRAPHS"] = "1"
    return env


def _build_cmd(prompt: str, negative: str | None, seed: int, output: Path,
               width: int, height: int, frames: int,
               fps: int | None = None, cfg: float = 3.0,
               guidance: float = 3.5, sampler: str = "euler",
               sigmas: str | None = None) -> list[str]:
    cmd = [
        SDCPP_BIN,
        "-M", "vid_gen",
        "--diffusion-model", SDCPP_LTX25_DIFFUSION,
        "--vae", SDCPP_LTX25_VAE,
        "--llm", SDCPP_LTX25_LLM,
        "--backend", "te=ROCm0,diffusion=ROCm0,vae=ROCm0",
        "--params-backend", "te=cpu,diffusion=ROCm0,vae=ROCm0",
        "--max-vram", "15",
        "--steps", "8",
        "--cfg-scale", str(cfg),
        "--guidance", str(guidance),
        "--sampling-method", sampler,
        "--video-frames", str(frames),
        "--width", str(width),
        "--height", str(height),
        "--temporal-tiling",
        "--extra-tiling-args", "temporal_tile_frames=4",
        "--diffusion-fa",
        "--output", str(output),
        "-p", prompt,
        "-s", str(seed),
        "-v",
    ]
    if negative:
        cmd += ["-n", negative]
    if fps is not None:
        cmd += ["--fps", str(fps)]
    if sigmas is not None:
        cmd += ["--sigmas", sigmas]
    return cmd


async def run_case(case: str) -> bool:
    width, height, frames, seed, expect = CASES[case]
    fps = FPS_CASES.get(case)
    cfg, negative, guidance, sampler, sigmas = SPEC_CASES.get(
        case, (3.0, NEG_PROD, 3.5, "euler", None))
    name = f"hdvar_{case}_s{seed}"
    out = OUT / f"{name}.webm"
    with open(OUT / f"{name}.log", "w") as f:
        f.write(f"case={case} seed={seed} expect_hd={expect}\n")
        f.write(f"params={width}x{height} frames={frames} fps={fps} steps=8 "
                f"cfg={cfg} guidance={guidance} sampler={sampler} "
                f"sigmas={sigmas or '(sd-cli default)'} "
                f"negative={'yes' if negative else 'NO (positive-only)'}\n")
        f.write(f"prompt={PROMPT_CLIP3}\nnegative={negative or '(none)'}\n")
    log.info(f"[{case}] {width}x{height} frames={frames} fps={fps} seed={seed} "
             f"cfg={cfg} guidance={guidance} sampler={sampler} "
             f"neg={'y' if negative else 'N'} sigmas={'off' if not sigmas else 'spec'} "
             f"expect HD={expect} -> {out}")

    proc = await asyncio.create_subprocess_exec(
        *_build_cmd(PROMPT_CLIP3, negative, seed, out, width, height,
                    frames, fps, cfg, guidance, sampler, sigmas),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_env(),
    )
    buf = bytearray()
    try:
        async with asyncio.timeout(SDCPP_TIMEOUT):
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 300000:
                    buf = buf[-150000:]
    except TimeoutError:
        log.error(f"[{case}] TIMEOUT")
        proc.terminate()
        await proc.wait()
        return False

    ret = await proc.wait()
    with open(OUT / f"{name}.stdout.log", "w") as f:
        f.write(buf.decode(errors="replace"))
    if ret != 0 or not out.is_file():
        log.error(f"[{case}] exited {ret}")
        return False
    log.info(f"[{case}] DONE ({out.stat().st_size} bytes)")
    return True


async def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    cases = list(CASES) if which == "all" else [which.upper()]
    ok = 0
    for case in cases:
        if case not in CASES:
            print(f"unknown case {case}, expected one of {list(CASES)}")
            raise SystemExit(2)
        if await run_case(case):
            ok += 1
    print(f"DONE {ok}/{len(cases)}")
    raise SystemExit(0 if ok == len(cases) else 1)


if __name__ == "__main__":
    asyncio.run(main())
