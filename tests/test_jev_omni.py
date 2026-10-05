"""Tests de l'adaptateur Jev-Omni (helpers/jev_omni.py).

Jev-Omni est un modèle de DÉCISION, pas un modèle chat : la tête de décision est
une sonde linéaire (`linear.weight` / `linear.bias` + `mu` / `sd`) appliquée à
l'état caché du dernier token. llama.cpp doit donc tourner en `--embedding
--pooling none` et on lit `/embedding`, pas `/v1/chat/completions`.

Ces tests verrouillent ce qui peut casser silencieusement :
- le format EXACT du prompt (le template du transformers amont) — une dérive
  ici ne lève aucune exception, le modèle répond juste n'importe quoi ;
- l'absence de `<bos>` littéral (llama.cpp l'insère déjà : double BOS silencieux) ;
- les bornes 2..256 options exigées par Jev-Omni ;
- l'arithmétique de la tête et la normalisation softmax ;
- le fait qu'une erreur Jev ne fait PAS rejeter l'image (c'est Telegram qui tranche).

Aucun serveur, aucun GPU — la tête est testée sur un `.npz` fabriqué.
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from helpers import jev_omni
from helpers.jev_omni import (
    HEADCOUNT_ACCEPT,
    HEADCOUNT_MIN_CONFIDENCE,
    HIDDEN_SIZE,
    _build_prompt,
    _head_cache,
    classify_image_choice,
    headcount_on_image,
)

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok  {label}")
    else:
        failures.append(label)
        print(f"  KO  {label}")


def _make_head(tmp: Path, n_options: int, bias_row: int, n_dims: int = HIDDEN_SIZE) -> Path:
    """Fabrique une tête de décision controlable.

    `bias_row` force la réponse : on met un grand logit sur cette ligne pour
    savoir exactamente quelle option l'argmax doit choisir.
    """
    rng = np.random.default_rng(0)
    path = tmp / f"head_{n_options}_{bias_row}.npz"
    # `np.savez` n'accepte pas de kwarg contenant un point : on passe un dict.
    # Les noms "linear.weight" etc. sont imposés par le .npz officiel.
    n_opts = n_options
    base = {
        "linear.weight": rng.normal(size=(n_opts, n_dims)).astype(np.float32),
        "linear.bias": np.zeros(n_opts, dtype=np.float32),
        "mu": np.zeros(n_dims, dtype=np.float32),
        "sd": np.ones(n_dims, dtype=np.float32),
    }
    base["linear.bias"][bias_row] = 50.0
    np.savez(path, **base)
    return path


def main() -> int:
    print("[1] format du prompt (template transformers amont)")
    prompt = _build_prompt("STATE", "QUESTION?", ["a", "b"], "<image>")
    check("<image>STATE" in prompt, "le marqueur média précède l'état")
    check("<|turn>user\n" in prompt, "ouverture du tour user")
    check("<|channel>thought\n<channel|>" in prompt, "préfixe thought-channel final")
    check("<bos>" not in prompt, "pas de <bos> littéral (double BOS silencieux)")
    check("1. a" in prompt and "2. b" in prompt, "options numérotées à partir de 1")
    check("only the number" in prompt, "instruction de réponse")
    check("Reply with only the number of the correct option (1-2)." in prompt,
          "borne haute des options dans l'instruction")
    print("[1] format du prompt : OK")

    print("\n[2] bornes du nombre d'options (Jev-Omni exige 2..256)")
    check(_build_prompt("S", "Q", ["a"], "<image>") is not None, "1 option: prompt construit")
    # Les bornes sont validées dans classify_image_choice, pas dans _build_prompt.
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "x.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        res = asyncio_run(classify_image_choice(str(img), "Q", ["seule"]))
        check(res.get("ok") is False and "2..256" in res.get("reason", ""),
              "1 option rejetée par l'API")
        res = asyncio_run(classify_image_choice(str(img), "Q", [str(i) for i in range(257)]))
        check(res.get("ok") is False and "2..256" in res.get("reason", ""),
              "257 options rejetées par l'API")

        # Image illisible / absente : on ne doit jamais lever.
        res = asyncio_run(classify_image_choice(str(Path(td) / "absent.png"), "Q", ["a", "b"]))
        check(res.get("ok") is False and "illisible" in res.get("reason", ""),
              "image absente -> ok:False, pas d'exception")
    print("[2] bornes des options : OK")

    print("\n[3] arithmétique de la tête de décision")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        original_head = jev_omni.JEV_OMNI_HEAD
        original_cache = jev_omni._HEAD_CACHE
        try:
            head_path = _make_head(tmp, n_options=3, bias_row=1)
            jev_omni.JEV_OMNI_HEAD = str(head_path)
            jev_omni._HEAD_CACHE = None
            head = _head_cache()
            check(head["weight"].shape == (3, HIDDEN_SIZE),
                  f"poids de tête {head['weight'].shape} == (3, {HIDDEN_SIZE})")
            check(head["sd"].shape == (HIDDEN_SIZE,), "sd broadcastable sur hidden")

            # Reproduit le calcul de classify_image_choice hors réseau.
            hidden = np.zeros(HIDDEN_SIZE, dtype=np.float32)
            z = head["weight"][:3] @ ((hidden - head["mu"]) / head["sd"])
            z = z + head["bias"][:3]
            z = z - z.max()
            probs = np.exp(z)
            probs = probs / probs.sum()
            check(abs(float(probs.sum()) - 1.0) < 1e-6, "softmax normalisée à 1")
            check(int(probs.argmax()) == 1, "argmax sur la ligne forcee (bias=50)")
            check(float(probs[1]) > 0.99, "confiance de la ligne forcée > 0.99")
            check(float(probs[0]) < 1e-9 and float(probs[2]) < 1e-9,
                  "probabilités concurrentes quasi nulles")
        finally:
            jev_omni.JEV_OMNI_HEAD = original_head
            jev_omni._HEAD_CACHE = original_cache
    print("[3] arithmétique de la tête : OK")

    print("\n[4] porte headcount : seuils et non-rejet sur erreur modèle")
    original = jev_omni.classify_image_choice

    jev_omni.classify_image_choice = awaitable_stub(
        lambda: {"ok": True, "prediction": HEADCOUNT_ACCEPT, "confidence": 0.9,
                 "probabilities": {}})
    res = asyncio_run(headcount_on_image("x.png"))
    check(res.get("verdict") == "accept", "headcount 'one' à 0.90 -> accept")

    jev_omni.classify_image_choice = awaitable_stub(
        lambda: {"ok": True, "prediction": HEADCOUNT_ACCEPT,
                 "confidence": HEADCOUNT_MIN_CONFIDENCE - 0.01, "probabilities": {}})
    res = asyncio_run(headcount_on_image("x.png"))
    check(res.get("verdict") == "reject", "headcount 'one' sous le seuil -> reject")

    jev_omni.classify_image_choice = awaitable_stub(
        lambda: {"ok": True, "prediction": "two or more", "confidence": 0.95,
                 "probabilities": {}})
    res = asyncio_run(headcount_on_image("x.png"))
    check(res.get("verdict") == "reject", "headcount 'two or more' -> reject")

    jev_omni.classify_image_choice = awaitable_stub(
        lambda: {"ok": False, "reason": "serveur indisponible"})
    res = asyncio_run(headcount_on_image("x.png"))
    check(res.get("ok") is False, "erreur modèle -> ok:False (l'appelant ne rejette pas)")
    check("indisponible" in str(res), "la raison est propagée pour la caption Telegram")
    jev_omni.classify_image_choice = original
    print("[4] porte headcount : OK")

    print("\n[5] modèles présents / serveur indisponible")
    # On pointe les chemins vers un dossier vide : models_present() doit être False
    # sans rien lancer, et le test ne doit pas dépendre de ce qui est déjà
    # téléchargé sur la machine.
    saved_paths = (jev_omni.JEV_OMNI_GGUF, jev_omni.JEV_OMNI_MMPROJ, jev_omni.JEV_OMNI_HEAD)
    saved_is_up, saved_proc = jev_omni._is_up, jev_omni._SERVER_PROC
    empty = str(Path(tempfile.gettempdir()) / "jev_omni_absent_dir")

    async def _server_down() -> bool:
        return False

    try:
        jev_omni.JEV_OMNI_GGUF = f"{empty}/m.gguf"
        jev_omni.JEV_OMNI_MMPROJ = f"{empty}/mm.gguf"
        jev_omni.JEV_OMNI_HEAD = f"{empty}/h.npz"
        check(jev_omni.models_present() is False,
              "chemins absents -> models_present() False")
        # ensure_server() interroge _is_up() AVANT models_present() : si une
        # instance tourne deja sur le port, elle est reutilisee. On neutralise
        # _is_up pour que le test ne depende pas d'un serveur deja demarre.
        jev_omni._is_up = _server_down
        ready = asyncio_run(jev_omni.ensure_server())
        check(ready is False,
              "ensure_server() False sans modèles, pas d'exception ni process lancé")
        check(jev_omni._SERVER_PROC is saved_proc,
              "aucun process lance quand les modeles manquent")
    finally:
        (jev_omni.JEV_OMNI_GGUF, jev_omni.JEV_OMNI_MMPROJ,
         jev_omni.JEV_OMNI_HEAD) = saved_paths
        jev_omni._is_up, jev_omni._SERVER_PROC = saved_is_up, saved_proc
    print("[5] garde-fous serveur : OK")

    if failures:
        print(f"\nECHECS : {len(failures)}")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\nTOUS LES TESTS PASSENT")
    return 0


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def awaitable_stub(factory):
    """Remplace classify_image_choice par une coroutine qui renvoie un dict figé."""
    async def _inner(image_path, question=None, options=None, state=""):
        return factory()
    return _inner


if __name__ == "__main__":
    raise SystemExit(main())