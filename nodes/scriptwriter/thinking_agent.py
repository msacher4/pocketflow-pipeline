"""Thinking Agent — réflexion profonde avant script generation.

Phase 1 du pipeline alt : qwen3.8-27b lit l'article, réfléchit librement,
fait des recherches web DuckDuckGo, et produit une IDÉE DE VIDÉO structurée.

Architecture : agent loop (pas de nombre fixe de searches).
Le modèle décide quand il a assez d'informations.
"""
import json
import logging
import re

import httpx
from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_MODEL, LLM_URL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import load_soul, load_knowledge, _extract_json, _trace_llm, LLMJSONQuoteError
from helpers.web_search import web_search, format_search_results

log = logging.getLogger("pocketflow-pipeline")

MAX_SEARCHES = 10
SEARCH_MARKER = "[SEARCH]"
TIMEOUT_PER_LLM_CALL = 600  # 10 min max par appel LLM
TIMEOUT_AGENT_TOTAL = 1800  # 30 min max pour tout l'agent

_THINKING_FIELDS = (
    "video_idea", "why_it_works", "target_audience", "affiliate_angle",
    "search_insights", "visual_concept", "viral_mechanism",
    "spectator_stake", "chosen_structure", "structure_why",
)


def _salvage_thinking(raw: str) -> dict:
    """Dernier recours quand le JSON est irréparable : extrait chaque champ par
    regex tolérante (la valeur se termine au prochain `"champ":` ou `}`), pour
    toujours afficher un rapport multi-points plutôt qu'un blob JSON brut dans
    `video_idea` (cf. run du 21/09 : guillemets internes non échappés)."""
    out = {f: "" for f in _THINKING_FIELDS}
    text = raw or ""
    for f in _THINKING_FIELDS:
        m = re.search(r'"' + re.escape(f) + r'"\s*:\s*"', text)
        if not m:
            continue
        start = m.end()
        end_m = re.search(r'"\s*,\s*"\w+"\s*:|"\s*\}\s*$', text[start:])
        val = text[start:start + end_m.start()] if end_m else text[start:start + 2000]
        out[f] = val.strip()
    if not out["video_idea"]:
        out["video_idea"] = text[:2000]
    return out


async def _call_llm_messages(model: str, messages: list[dict], timeout: int = TIMEOUT_PER_LLM_CALL, max_tokens: int = 16384) -> str:
    """Appel LLM avec historique de conversation (multi-turn).

    qwen3.8 est un modèle de raisonnement : il produit d'abord
    `reasoning_content` (réflexion + marqueurs [SEARCH]) puis le `content`
    final (le JSON). On combine les deux pour la détection des recherches,
    mais le JSON final est extrait du `content`.
    """
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


class ThinkingAgentNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "thinking_agent"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("thinking_agent")
        structures = load_knowledge("waifu_structures")
        if structures.strip():
            soul += (
                "\n\n===== STRUCTURES D'INSPIRATION DISPONIBLES =====\n"
                f"{structures}\n"
                "===== FIN STRUCTURES =====\n\n"
                "Choisis LA structure la plus pertinente pour CETTE actu et mets son "
                "nom EXACT dans `chosen_structure`, avec ta justification dans `structure_why`."
            )
        article = shared.get("selected_article", {})
        article_content = article.get("synthesis") or article.get("description") or article.get("summary") or ""
        character = article.get("character") or {}

        user_msg = (
            f"Thème: {shared.get('topic', '')}\n"
            f"Article: {article.get('title', '')}\n"
            f"Source: {article.get('source', '')}\n"
            f"URL: {article.get('url', '')}\n"
        )
        if character.get("name"):
            user_msg += (
                f"\nPersonnage central: {character.get('name')} "
                f"({character.get('franchise', '')})\n"
            )
        user_msg += (
            f"\n--- CONTENU DE L'ARTICLE ---\n"
            f"{article_content[:5000]}\n"
            f"--- FIN ---\n\n"
            f"Réfléchis à comment en faire une vidéo TikTok qui cartonne. "
            f"Utilise la recherche web autant que nécessaire. "
            f"Quand tu as fini, produis le JSON final."
        )

        messages = [
            {"role": "system", "content": soul},
            {"role": "user", "content": user_msg},
        ]

        search_count = 0
        last_response = ""
        last_content = ""
        forced_first_search = False

        log.info(f"ThinkingAgent: starting reflection for '{article.get('title', '')[:60]}'")

        for iteration in range(MAX_SEARCHES + 1):
            response = await _call_llm_messages(LLM_SCRIPTWRITER_ALT_MODEL, messages)
            try:
                envelope = json.loads(response)
                reasoning = envelope.get("reasoning", "")
                content = envelope.get("content", "")
            except (json.JSONDecodeError, TypeError):
                reasoning = ""
                content = response
            last_response = content
            last_content = content

            # Conserver le reasoning dans l'historique si non vide
            if reasoning.strip():
                messages.append({"role": "assistant", "content": content, "reasoning_content": reasoning})
            else:
                messages.append({"role": "assistant", "content": content})

            # Chercher des [SEARCH] UNIQUEMENT dans le content final, jamais
            # dans le reasoning_content (le modèle de raisonnement y reproduit
            # les lignes [SEARCH] de l'instruction → recherches absurdes).
            searches = re.findall(r'\[SEARCH\]\s*(.+?)(?:\n|$)', content)

            if not searches:
                # Première passe SANS recherche : forcer une recherche avant de
                # laisser le modèle produire son JSON final.
                if search_count == 0 and not forced_first_search:
                    forced_first_search = True
                    log.info("ThinkingAgent: no search yet, forcing a first web search")
                    messages.append({"role": "user", "content": (
                        "Tu ne peux PAS produire le JSON final sans avoir fait AU MOINS "
                        "UNE recherche web. Commence par chercher : ta réponse ENTIÈRE doit "
                        "être uniquement la ligne [SEARCH] ta requête. Ensuite analyse les "
                        "résultats et réfléchis, puis cherche encore si nécessaire avant "
                        "de produire le JSON final."
                    )})
                    continue
                log.info(f"ThinkingAgent: no more searches, producing final output (iteration {iteration})")
                break

            # Exécuter les recherches
            for query in searches:
                if search_count >= MAX_SEARCHES:
                    log.info(f"ThinkingAgent: max searches ({MAX_SEARCHES}) reached")
                    break
                query = query.strip()
                log.info(f"ThinkingAgent: search [{search_count+1}] '{query}'")
                results = web_search(query, limit=5)
                formatted = format_search_results(results, query)
                search_count += 1

                # Feed les résultats au LLM
                search_feedback = (
                    f"Résultats de recherche pour '{query}':\n\n"
                    f"{formatted}\n\n"
                    f"Analyse ces résultats et continue ta réflexion."
                )
                messages.append({"role": "user", "content": search_feedback})

            if search_count >= MAX_SEARCHES:
                log.info(f"ThinkingAgent: max searches reached, requesting final output")
                messages.append({"role": "user", "content": (
                    "Tu as fait assez de recherches. Maintenant produis le JSON final "
                    "avec ta réflexion complète."
                )})
                # Un dernier appel pour le JSON final
                last_response = await _call_llm_messages(LLM_SCRIPTWRITER_ALT_MODEL, messages)
                try:
                    envelope = json.loads(last_response)
                    reasoning = envelope.get("reasoning", "")
                    last_response = envelope.get("content", "")
                    last_content = last_response
                except (json.JSONDecodeError, TypeError):
                    last_content = last_response
                messages.append({"role": "assistant", "content": last_response})
                break

        # Parser le JSON final
        decision = None
        try:
            # Nettoyer les tags XML avant de parser
            clean_response = re.sub(r'<tool_call>.*?</tool_call>', '', last_content, flags=re.DOTALL)
            clean_response = re.sub(r'<web_search>.*?</web_search>', '', clean_response, flags=re.DOTALL)
            decision = _extract_json(clean_response)
        except LLMJSONQuoteError as e:
            # Le modèle a mis des guillemets doubles dans les valeurs : 1 appel
            # correctif ciblé (même pattern que l'AssetPlanner) au lieu de
            # dégrader en silence vers le fallback brut.
            log.warning(f"ThinkingAgent: JSON quote error, requesting clean re-emit: {e}")
            messages.append({"role": "user", "content": (
                "Ton JSON est invalide : tu as écrit des guillemets doubles \" à "
                "l'INTÉRIEUR des valeurs texte. Réémet EXACTEMENT le même JSON, "
                "sans rien changer au contenu, mais en remplaçant TOUS les "
                "guillemets doubles internes par des apostrophes simples '. Les "
                "guillemets doubles ne servent qu'à délimiter les clés et valeurs, "
                "jamais dans le texte. Retourne UNIQUEMENT le JSON corrigé."
            )})
            try:
                retry_resp = await _call_llm_messages(LLM_SCRIPTWRITER_ALT_MODEL, messages)
                try:
                    envelope = json.loads(retry_resp)
                    retry_content = envelope.get("content", "") or retry_resp
                except (json.JSONDecodeError, TypeError):
                    retry_content = retry_resp
                clean_retry = re.sub(r'<tool_call>.*?</tool_call>', '', retry_content, flags=re.DOTALL)
                clean_retry = re.sub(r'<web_search>.*?</web_search>', '', clean_retry, flags=re.DOTALL)
                decision = _extract_json(clean_retry)
                last_content = retry_content
                log.info("ThinkingAgent: clean re-emit parsed OK")
            except Exception as e2:
                log.warning(f"ThinkingAgent: re-emit still invalid ({e2}), salvage fallback")
                decision = None
        except Exception as e:
            log.warning(f"ThinkingAgent: JSON parse failed: {e}")
            decision = None

        if decision is None:
            # Fallback qui sauve les meubles : champs extraits un par un plutôt
            # que le JSON brut collé dans video_idea (Telegram garde son rapport
            # multi-points, même partiel).
            decision = _salvage_thinking(last_content)

        log.info(f"ThinkingAgent: done, video_idea={decision.get('video_idea', '')[:100]}... "
                 f"structure={decision.get('chosen_structure', '')}")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except (json.JSONDecodeError, TypeError):
            decision = {"video_idea": str(exec)[:2000]}

        shared["thinking_agent"] = decision
        log.info(f"ThinkingAgent POST -> video_idea saved to shared['thinking_agent']")
        return "default"
