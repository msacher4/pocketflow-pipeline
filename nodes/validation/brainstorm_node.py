"""BrainstormValidationNode — session multi-tours avec Thinking Agent via Telegram.

L'utilisateur reçoit l'output du Thinking Agent, peut répondre pour brainstormer,
et le Thinking Agent révise sa réflexion en fonction des feedbacks.
Quand l'utilisateur clique "Validate", la session se termine.
"""
import asyncio
import json
import logging
import re

from pocketflow import AsyncNode

from config import TG_BOT_TOKEN, LLM_SCRIPTWRITER_ALT_MODEL, LLM_URL
from helpers.state import (
    _set_state, _shared_snapshot,
    _register_validation, _pending_validations, _attach_message,
)
from helpers.send_telegram import send_telegram
from helpers.call_llm import load_soul, _extract_json, _trace_llm
from helpers.web_search import web_search, format_search_results

import httpx

log = logging.getLogger("pocketflow-pipeline")

BRAINSTORM_TIMEOUT = 1800  # 30 min


def _clean_idea(text: str) -> str:
    """Garde-fou d'affichage : si video_idea ressemble à du JSON brut
    (résidu d'un parse raté), extrait la valeur au lieu d'afficher le blob."""
    t = (text or "").strip()
    m = re.match(r'^\{\s*"video_idea"\s*:\s*"(.*)', t, re.DOTALL)
    if not m:
        return text
    t = m.group(1)
    tail = re.search(r'"\s*,\s*"\w+"\s*:.*\}\s*$', t, re.DOTALL)
    if tail:
        t = t[:tail.start()]
    return t.rstrip().rstrip('"').strip() or text


def _format_thinking_message(thinking: dict) -> str:
    """Formate l'output du Thinking Agent pour Telegram."""
    parts = ["🧠 Thinking Agent — Réflexion\n"]

    if thinking.get("video_idea"):
        parts.append(f"📝 Idée de vidéo:\n{_clean_idea(thinking['video_idea'])}\n")
    if thinking.get("why_it_works"):
        parts.append(f"💡 Pourquoi ça marcherait:\n{thinking['why_it_works']}\n")
    if thinking.get("target_audience"):
        parts.append(f"🎯 Public cible:\n{thinking['target_audience']}\n")
    if thinking.get("affiliate_angle"):
        parts.append(f"💰 Angle affiliation:\n{thinking['affiliate_angle']}\n")
    if thinking.get("search_insights"):
        parts.append(f"🔍 Insights recherches:\n{thinking['search_insights']}\n")
    if thinking.get("visual_concept"):
        parts.append(f"🎬 Concept visuel:\n{thinking['visual_concept']}\n")

    parts.append("━━━━━━━━━━━━━━━━━━━━")
    parts.append("💬 Réponds à ce message pour brainstormer")
    parts.append("✅ clique \"Validate\" quand c'est bon")

    return "\n".join(parts)


def _format_revised_message(thinking: dict, turn: int) -> str:
    """Formate une révision du thinking pour Telegram."""
    parts = [f"🧠 Thinking Agent — Révision #{turn}\n"]

    if thinking.get("video_idea"):
        parts.append(f"📝 Idée de vidéo:\n{_clean_idea(thinking['video_idea'])}\n")
    if thinking.get("why_it_works"):
        parts.append(f"💡 Pourquoi ça marcherait:\n{thinking['why_it_works']}\n")
    if thinking.get("target_audience"):
        parts.append(f"🎯 Public cible:\n{thinking['target_audience']}\n")
    if thinking.get("affiliate_angle"):
        parts.append(f"💰 Angle affiliation:\n{thinking['affiliate_angle']}\n")
    if thinking.get("search_insights"):
        parts.append(f"🔍 Insights recherches:\n{thinking['search_insights']}\n")
    if thinking.get("visual_concept"):
        parts.append(f"🎬 Concept visuel:\n{thinking['visual_concept']}\n")

    parts.append("━━━━━━━━━━━━━━━━━━━━")
    parts.append("💬 Continue à brainstormer ou clique \"Validate\"")

    return "\n".join(parts)


async def _call_llm_multi(model: str, messages: list[dict], timeout: int = 600, max_tokens: int = 16384) -> str:
    """Appel LLM multi-turn (qwen3.8: reasoning_content + content combinés)."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(LLM_URL, json=body)
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        reasoning = msg.get("reasoning_content") or ""
        content = msg.get("content") or ""
        return json.dumps({"reasoning": reasoning, "content": content}, ensure_ascii=False)


class BrainstormValidationNode(AsyncNode):
    """Session multi-tours : l'utilisateur brainstorm avec le Thinking Agent."""

    def __init__(self, timeout: int = BRAINSTORM_TIMEOUT):
        super().__init__(max_retries=1, wait=5)
        self.timeout = timeout

    async def prep_async(self, shared):
        shared["_current_step"] = "brainstorm_thinking"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        thinking = shared.get("thinking_agent", {})
        article = shared.get("selected_article", {})
        pipeline_id = shared.get("pipeline_id", "unknown")
        vid = f"bs_{str(pipeline_id)[-12:]}_pf"

        if not TG_BOT_TOKEN:
            log.info("[tg] SKIP brainstorm (no token)")
            return json.dumps(thinking, ensure_ascii=False)

        # 1. Envoyer l'initial thinking à Telegram
        text = _format_thinking_message(thinking)
        buttons = [[{"text": "✅ Validate", "callback_data": f"brainstorm_validate:{vid}"}]]
        event = _register_validation(vid)
        msg_id = await send_telegram(text, buttons)
        if msg_id:
            _attach_message(vid, msg_id)

        log.info(f"Brainstorm: sent initial thinking, waiting for user input")

        # 2. Construire le prompt système pour la session brainstorm
        soul = load_soul("thinking_agent")
        article_content = article.get("synthesis") or article.get("description") or article.get("summary") or ""
        character = article.get("character") or {}

        initial_ctx = (
            f"Tu as déjà produit cette réflexion:\n\n"
            f"{json.dumps(thinking, ensure_ascii=False, indent=2)}\n\n"
            f"L'utilisateur va maintenant te donner son feedback pour améliorer ta réflexion. "
            f"Adapte ta réflexion en fonction de ses remarques. "
            f"Tu peux modifier, ajouter ou supprimer n'importe quelle partie de ta réflexion. "
            f"Tu peux aussi faire des recherches web supplémentaires si nécessaire "
            f"(utilise [SEARCH] query comme avant). "
            f"Quand tu as fini, produis le JSON révisé avec les mêmes clés."
        )

        # Historique de conversation pour le LLM
        llm_messages = [
            {"role": "system", "content": soul},
            {"role": "user", "content": initial_ctx},
            {"role": "assistant", "content": json.dumps(thinking, ensure_ascii=False)},
        ]

        # 3. Boucle multi-tours
        turn = 0
        current_thinking = thinking

        while True:
            try:
                await asyncio.wait_for(event.wait(), timeout=self.timeout)
            except asyncio.TimeoutError:
                log.info("Brainstorm: timeout after 30 min")
                break

            entry = _pending_validations.pop(vid, {})
            result = entry.get("result", "")

            if result == "brainstorm_validate":
                log.info("Brainstorm: user validated")
                break

            if result.startswith("feedback:"):
                user_text = result[len("feedback:"):]
                turn += 1
                log.info(f"Brainstorm: user feedback #{turn}: {user_text[:200]}")

                # Ajouter le feedback à l'historique LLM
                llm_messages.append({"role": "user", "content": user_text})

                # Appeler le LLM pour réviser la réflexion (avec boucle searches)
                search_count = 0
                for _ in range(5):  # max 5 searches par tour
                    response = await _call_llm_multi(LLM_SCRIPTWRITER_ALT_MODEL, llm_messages)
                    try:
                        envelope = json.loads(response)
                        reasoning = envelope.get("reasoning", "")
                        response = envelope.get("content", "")
                    except (json.JSONDecodeError, TypeError):
                        reasoning = ""
                    if reasoning.strip():
                        llm_messages.append({"role": "assistant", "content": response, "reasoning_content": reasoning})
                    else:
                        llm_messages.append({"role": "assistant", "content": response})

                    # Chercher des [SEARCH] UNIQUEMENT dans le content final,
                    # jamais dans le reasoning_content.
                    searches = re.findall(r'\[SEARCH\]\s*(.+?)(?:\n|$)', response)
                    if not searches:
                        break

                    for query in searches:
                        if search_count >= 5:
                            break
                        query = query.strip()
                        log.info(f"Brainstorm: search '{query}'")
                        results = web_search(query, limit=3)
                        formatted = format_search_results(results, query)
                        search_count += 1
                        llm_messages.append({
                            "role": "user",
                            "content": f"Résultats pour '{query}':\n{formatted}\n\nContinue."
                        })

                # Parser le JSON révisé
                try:
                    # Nettoyer les tags XML avant de parser
                    clean_response = re.sub(r'<tool_call>.*?</tool_call>', '', response, flags=re.DOTALL)
                    clean_response = re.sub(r'<web_search>.*?</web_search>', '', clean_response, flags=re.DOTALL)
                    revised = _extract_json(clean_response)
                    current_thinking = revised
                    log.info(f"Brainstorm: revised thinking #{turn}")
                except Exception as e:
                    log.warning(f"Brainstorm: JSON parse failed for revision #{turn}: {e}")
                    # Garder le thinking précédent

                # Envoyer la révision à Telegram
                revised_text = _format_revised_message(current_thinking, turn)
                event = _register_validation(vid)
                msg_id = await send_telegram(revised_text, buttons)
                if msg_id:
                    _attach_message(vid, msg_id)

        # 4. Stocker le résultat final
        log.info(f"Brainstorm: done after {turn} turns")
        return json.dumps(current_thinking, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            thinking = json.loads(exec)
        except (json.JSONDecodeError, TypeError):
            thinking = shared.get("thinking_agent", {})

        shared["thinking_agent"] = thinking
        shared["_current_step"] = "brainstorm_thinking_done"
        shared["steps"].append({
            "step": "brainstorm_thinking", "status": "ok",
            "ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })
        await _set_state(**_shared_snapshot(shared))
        return "default"
