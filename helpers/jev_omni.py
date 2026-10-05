import asyncio
import base64
import logging
import os
import subprocess
import time

import httpx
import numpy as np

from config import (
    JEV_OMNI_BIN,
    JEV_OMNI_GGUF,
    JEV_OMNI_HEAD,
    JEV_OMNI_MMPROJ,
    JEV_OMNI_TIMEOUT,
    JEV_OMNI_URL,
)

log = logging.getLogger("pocketflow-pipeline")

# Jev-Omni est un modèle de décision, PAS un modèle chat : la tête de décision
# est une sonde linéaire (linear.weight/bias + mu/sd) appliquée à l'état caché du
# dernier token. llama.cpp doit donc tourner en --embedding --pooling none et on
# lit /embedding — pas /v1/chat/completions. hidden_size du modèle = 3840.
HIDDEN_SIZE = 3840

# Waken comme rewrite_i2v_prompt, mais sur une instance dédiée (port 8977) : le
# proxy chat 8080 n'expose pas /embedding.
_SERVER_PROC = None
_WAKE_LOCK = asyncio.Lock()


def _head_cache():
    global _HEAD_CACHE
    if _HEAD_CACHE is None:
        with np.load(JEV_OMNI_HEAD) as head:
            _HEAD_CACHE = {
                "weight": np.asarray(head["linear.weight"], dtype=np.float32),
                "bias": np.asarray(head["linear.bias"], dtype=np.float32),
                "mu": np.asarray(head["mu"], dtype=np.float32).reshape(-1),
                "sd": np.asarray(head["sd"], dtype=np.float32).reshape(-1),
            }
    return _HEAD_CACHE


_HEAD_CACHE = None


def models_present() -> bool:
    return all(
        os.path.isfile(p) for p in (JEV_OMNI_GGUF, JEV_OMNI_MMPROJ, JEV_OMNI_HEAD)
    )


async def _is_up() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{JEV_OMNI_URL}/health")
            return r.status_code == 200
    except Exception:
        return False


async def ensure_server() -> bool:
    """Démarre le llama-server dédié s'il ne répond pas. Vrai si joignable."""
    global _SERVER_PROC
    if await _is_up():
        return True
    if not models_present():
        log.warning(
            "jev_omni: modèles absents (gguf/mmproj/head) — vérifie %s", JEV_OMNI_GGUF
        )
        return False
    async with _WAKE_LOCK:
        if await _is_up():
            return True
        # ROCm est OBLIGATOIRE, et le projecteur vision doit rester sur le CPU. Deux
        # fautes de numerique distinctes, mesurees sur ce GGUF :
        #   - CPU pur (-ngl 0)      -> 55x3840 de NaN, y compris SANS image ;
        #   - -ngl 99 seul         -> texte OK (norme 134.4) mais image 73x3840 NaN.
        # Avec --no-mmproj-offload en plus, l'image sort propre (norme 138.6).
        # Donc : LLM sur ROCm, mmproj sur CPU.
        # -c 8192 : n_ctx par defaut = 262144, inutile et ruineux pour un prompt
        # image + une question.
        cmd = [
            JEV_OMNI_BIN,
            "-m", JEV_OMNI_GGUF,
            "--mmproj", JEV_OMNI_MMPROJ,
            "--embedding",
            "--pooling", "none",
            "--host", "127.0.0.1",
            "--port", JEV_OMNI_URL.rsplit(":", 1)[-1],
            "-ngl", "99",
            "-c", "8192",
            "--no-mmproj-offload",
        ]
        log.info("jev_omni: démarrage llama-server embedding sur %s", JEV_OMNI_URL)
        try:
            _SERVER_PROC = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            log.warning("jev_omni: démarrage impossible: %s", e)
            return False
        for _ in range(90):
            await asyncio.sleep(2)
            if await _is_up():
                log.info("jev_omni: serveur prêt")
                return True
            if _SERVER_PROC.poll() is not None:
                log.warning("jev_omni: llama-server mort au démarrage (code %s)",
                            _SERVER_PROC.returncode)
                return False
    log.warning("jev_omni: serveur pas prêt après 180s")
    return False


def _build_prompt(state: str, question: str, options: list[str], marker: str) -> str:
    choices = "\n".join(f"{i + 1}. {value}" for i, value in enumerate(options))
    text = (
        f"{state}\n\n---\n\nQUESTION: {question}\n\nOPTIONS:\n{choices}\n\n"
        f"Reply with only the number of the correct option (1-{len(options)}).\n"
        "Output a single number and nothing else."
    )
    # Template chat du transformers amont. llama.cpp injecte BOS tout seul : ne pas
    # envoyer <bos> ici sinon double BOS silencieux. Le préfixe thought-channel
    # est requis pour que l'état caché tombe sur le bon token.
    return f"<|turn>user\n{marker}{text}<turn|>\n<|turn>model\n<|channel>thought\n<channel|>"


async def classify_image_choice(
    image_path: str, question: str, options: list[str], state: str = ""
) -> dict:
    """Jev-Omni sur une IMAGE : renvoie {'ok': True, 'prediction', 'confidence',
    'probabilities'} ou {'ok': False, 'reason': ...} — jamais d'exception.

    Contrairement à typesafe/jev-1.13 (text-only sur l'API OpenRouter
    alpha/decisions), Jev-Omni passe le mmproj et voit réellement l'image.
    2 à 256 options. Le premier appel démarre le serveur dédié si besoin.
    """
    if not 2 <= len(options) <= 256:
        return {"ok": False, "reason": "options must be 2..256"}
    try:
        with open(image_path, "rb") as f:
            media = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        return {"ok": False, "reason": f"image illisible: {e}"}
    if not media:
        return {"ok": False, "reason": "image vide"}

    if not await ensure_server():
        return {"ok": False, "reason": "serveur jev_omni indisponible"}

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            props = (await c.get(f"{JEV_OMNI_URL}/props")).json()
        if not (props.get("modalities", {}) or {}).get("vision"):
            return {"ok": False, "reason": "serveur sans modality vision"}
        marker = props["media_marker"]
    except Exception as e:
        return {"ok": False, "reason": f"props: {str(e)[:160]}"}

    prompt = _build_prompt(state, question, options, marker)
    payload = {
        "content": {"prompt_string": prompt, "multimodal_data": [media]},
        "embd_normalize": -1,
    }
    try:
        async with httpx.AsyncClient(timeout=JEV_OMNI_TIMEOUT) as c:
            r = await c.post(f"{JEV_OMNI_URL}/embedding", json=payload)
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.warning("jev_omni: /embedding échoué: %s", str(e)[:200])
        return {"ok": False, "reason": str(e)[:200]}

    try:
        hidden = np.asarray(body[0]["embedding"][-1], dtype=np.float32)
        if hidden.shape != (HIDDEN_SIZE,):
            return {"ok": False, "reason": f"hidden {hidden.shape} != {HIDDEN_SIZE}"}
        head = _head_cache()
        z = head["weight"][: len(options)] @ ((hidden - head["mu"]) / head["sd"])
        z = z + head["bias"][: len(options)]
        z = z - z.max()
        probabilities = np.exp(z)
        probabilities = probabilities / probabilities.sum()
        best = int(probabilities.argmax())
    except Exception as e:
        return {"ok": False, "reason": f"head: {str(e)[:160]}"}

    return {
        "ok": True,
        "prediction": options[best],
        "prediction_index": best,
        "confidence": float(probabilities[best]),
        "probabilities": {
            opt: float(p) for opt, p in zip(options, probabilities)
        },
    }


HEADCOUNT_OPTIONS = ["one", "two or more", "no character"]
HEADCOUNT_QUESTION = "How many distinct characters appear in this image?"
HEADCOUNT_ACCEPT = "one"
# Seuil en bandes : les probabilités Jev varient d'un appel à l'autre, donc on
# juge sur une bande et pas sur une valeur exacte (même prudence que le
# benchmark Jev lui-même).
HEADCOUNT_MIN_CONFIDENCE = 0.55


async def headcount_on_image(image_path: str) -> dict:
    """Porte « un seul personnage » sur une image via Jev-Omni.

    Renvoie {'ok': True, 'verdict': 'accept'|'reject', 'confidence', 'choice',
    'probabilities'} ou {'ok': False, 'reason': ...}. En cas d'échec technique
    l'appelant décide — on ne rejette jamais sur une erreur de modèle.
    """
    res = await classify_image_choice(
        image_path, HEADCOUNT_QUESTION, HEADCOUNT_OPTIONS,
        state="A single anime/manga character reference photo.",
    )
    if not res.get("ok"):
        return {"ok": False, "reason": res.get("reason", "unknown")}
    choice = res["prediction"]
    confidence = res["confidence"]
    verdict = "accept" if (
        choice == HEADCOUNT_ACCEPT and confidence >= HEADCOUNT_MIN_CONFIDENCE
    ) else "reject"
    log.info(
        "jev_omni headcount: %s -> %s (%.2f) %s",
        os.path.basename(image_path), choice, confidence, verdict,
    )
    return {
        "ok": True,
        "verdict": verdict,
        "choice": choice,
        "confidence": confidence,
        "probabilities": res["probabilities"],
    }


def stop_server() -> dict:
    """Arrête le llama-server Jev pour libérer sa VRAM.

    Renvoie {'status': 'stopped'|'absent'|'failed', ...}. Ne lève jamais : un
    cleanup qui échoue ne doit pas faire tomber le run, et 'absent' est le cas
    normal quand le daemon redémarre (le serveur avait été lancé par le run
    précédent, pas par ce process).

    On se fie d'abord au handle _SERVER_PROC, mais on vérifie le port aussi : le
    serveur peut tourner alors qu'on n'est pas celui qui l'a démarré (lancement
    manuel, run précédent encore vivant).
    """
    global _SERVER_PROC
    results = {"pid": None, "signal": None, "port_still_up": None}

    proc = _SERVER_PROC
    if proc is not None and proc.poll() is None:
        results["pid"] = proc.pid
        try:
            proc.terminate()
            try:
                proc.wait(timeout=20)
                results["signal"] = "SIGTERM"
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                results["signal"] = "SIGKILL"
            log.info("jev_omni: llama-server arrêté (pid %s, %s)",
                     results["pid"], results["signal"])
        except Exception as e:
            results["error"] = f"{type(e).__name__}: {e}"
            log.warning("jev_omni: arrêt impossible: %s", e)

    # Handle absent ou déjà mort : le port décide si un serveur traîne encore.
    try:
        import httpx
        r = httpx.get(f"{JEV_OMNI_URL}/health", timeout=5)
        still_up = r.status_code == 200
    except Exception:
        still_up = False
    results["port_still_up"] = still_up

    _SERVER_PROC = None

    if results.get("error"):
        results["status"] = "failed"
    elif results["pid"] or not still_up:
        results["status"] = "stopped"
    else:
        # Joignable mais pas arrêté : ni handle ni signal n'ont agi. On le dit
        # plutôt que de le masquer, Klein va manquer de VRAM.
        results["status"] = "still_up"
    log.info("jev_omni: stop_server -> %s", results["status"])
    return results