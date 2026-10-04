import asyncio
import base64
import json
import logging
from datetime import datetime, timezone

import httpx
from pocketflow import AsyncNode

from config import LLM_URL, LLM_VISION_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import load_soul
from nodes.assetfinder.sdcpp_i2v_generator import _find_image_for_slot

log = logging.getLogger("pocketflow-pipeline")


class RewriteI2VPromptNode(AsyncNode):
    """Réécrit les prompts vidéo des slots I2V APRÈS la génération de l'image.

    Le prompt I2V d'origine est écrit À L'AVEUGLE (avant que l'image existe) :
    il décrit un visuel imaginé qui ne correspond pas à la frame 0 réelle -> le
    modèle vidéo déforme l'image pour « coller au prompt » au lieu d'animer ce
    qu'il reçoit. Ce node montre l'image de frame 0 au modèle vision
    (gemma4-12b) et réécrit le prompt pour qu'il décrive fidèlement cette image
    + un mouvement subtil cohérent avec la pose existante.

    L'image montrée n'est PAS un scan Danbooru : c'est l'image Klein générée
    en amont (ComfyUIKleinRefImageGenerator) depuis les 2 références
    Danbooru du slot. `_find_image_for_slot` la distingue désormais via le
    champ `source`, au lieu de se rabattre sur l'ordre d'insertion.
    """

    step = "rewrite_i2v_prompt"

    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "rewrite_i2v_prompt"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        slots = blueprint.get("slots", [])
        i2v_slots = [s for s in slots if s.get("mode") == "i2v"]
        if not i2v_slots:
            return json.dumps({"rewritten": []}, ensure_ascii=False)

        targets = shared.get("_i2v_regen_prompt_slots")
        if targets:
            i2v_slots = [s for s in i2v_slots if s.get("id") in targets]
            if not i2v_slots:
                return json.dumps({"rewritten": []}, ensure_ascii=False)

        soul = load_soul("rewrite_i2v_prompt")
        rewritten = []
        for slot in i2v_slots:
            slot_id = slot.get("id")
            image_path = _find_image_for_slot(shared, slot_id)
            old_prompt = slot.get("prompt", "")
            if not image_path or not old_prompt:
                log.warning(f"RewriteI2VPrompt: slot {slot_id} sans image/prompt, prompt inchangé")
                continue
            new_prompt = await self._rewrite_prompt(slot, image_path, old_prompt, soul)
            if new_prompt:
                slot["prompt"] = new_prompt
                rewritten.append({"slot_id": slot_id, "image_path": image_path})
        return json.dumps({"rewritten": rewritten}, ensure_ascii=False)

    async def _wake_vision(self) -> None:
        """Réveille le serveur vision via proxy-switch avant l'appel (idempotent).

        init_cleanup_alt tue tous les llama-server avant le rewrite ; le proxy
        dispose d'un endpoint /api/switch/<model> qui (re)charge le backend à la
        volée. Échec du wake = non bloquant : l'appel vision tente quand même.
        """
        base = LLM_URL.split("/v1/")[0]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{base}/api/switch/{LLM_VISION_MODEL}")
                resp.raise_for_status()
                log.info(f"RewriteI2VPrompt: vision {LLM_VISION_MODEL} réveillée via proxy-switch")
        except Exception as e:
            log.warning(f"RewriteI2VPrompt: wake vision {LLM_VISION_MODEL} non bloquant échoué: {e}")

    async def _rewrite_prompt(self, slot, image_path, old_prompt, soul) -> str:
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
        except OSError as e:
            log.warning(f"RewriteI2VPrompt: image illisible {image_path}: {e}")
            return ""
        if not b64:
            return ""

        ctx = (
            f"Prompt I2V d'origine (écrit à l'aveugle, peut être incohérent) :\n"
            f"{old_prompt}\n\n"
            f"L'image fournie est la frame 0 générée par Klein (et non un scan "
            f"Danbooru) : elle applique déjà la scène et le cadrage voulus.\n"
            f"Section du plan : {slot.get('section', '')} (position {slot.get('position', 0)})\n"
            f"Réécris le prompt UNIQUEMENT d'après CE QUE TU VOIS dans l'image fournie, "
            f"en injectant un mouvement subtil cohérent avec la pose existante (voir le soul)."
        )
        content = [
            {"type": "text", "text": f"{soul}\n\n{ctx}"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
        payload = {
            "model": LLM_VISION_MODEL,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 2048,
        }
        await self._wake_vision()
        data = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=180) as client:
                    resp = await client.post(LLM_URL, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                break
            except Exception as e:
                log.warning(f"RewriteI2VPrompt: appel vision échoué pour {slot.get('id')} "
                            f"(tentative {attempt + 1}/2): {e}")
                if attempt == 0:
                    await asyncio.sleep(20)
                    await self._wake_vision()
                else:
                    return ""
        if data is None:
            return ""
        text = (data.get("choices") or [{}])[0].get("message") or {}
        new_prompt = (text.get("content") or "").strip()
        if new_prompt:
            log.info(f"RewriteI2VPrompt: slot {slot.get('id')} prompt réécrit "
                     f"({len(new_prompt)} chars)")
        return new_prompt

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("RewriteI2VPrompt POST -> exec is not valid JSON")
            shared["_current_step"] = "rewrite_i2v_prompt_error"
            shared["_error"] = "RewriteI2VPrompt: exec is not valid JSON"
            shared["steps"].append({
                "step": "rewrite_i2v_prompt", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("RewriteI2VPrompt aborted: exec is not valid JSON")

        rewritten = data.get("rewritten", [])
        shared["_current_step"] = "rewrite_i2v_prompt_done"
        shared["steps"].append({
            "step": "rewrite_i2v_prompt", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(rewritten)} prompt(s) I2V réécrit(s)",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"