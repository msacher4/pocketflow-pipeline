import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces, _save_sub_shared
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm

log = logging.getLogger("pocketflow-pipeline")


class SynthesizeArticleNode(AsyncNode):
    """Synthétise le contenu brut de l'article sélectionné via LLM.

    Positionné APRÈS le fetch (r.jina.ai / browser) et AVANT extract_character.
    Produit une `synthesis` (idée principale propre) + un drapeau `has_character`
    qui décide si l'article est exploitable (centré sur un personnage féminin).

    Stocke le résultat dans selected_article["synthesis"] pour que les nodes en
    aval (extract_character, scriptwriter alt) s'appuient sur cette synthèse
    plutôt que sur le contenu brut (souvent verbeux / bruité).
    """

    def __init__(self):
        super().__init__(max_retries=2, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_synthesize"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("synthesize_article")
        article = shared.get("selected_article", {})
        # Priorité à la synthèse si déjà présente (rebouclage), sinon au contenu brut.
        content = (
            article.get("synthesis")
            or article.get("description")
            or article.get("summary")
            or ""
        )
        ctx = (
            f"Thème de la vidéo: {shared.get('topic', '')}\n"
            f"Titre de l'article: {article.get('title', '')}\n"
            f"Source: {article.get('source', '')}\n"
            f"\n--- CONTENU COMPLET DE L'ARTICLE ---\n"
            f"{content[:8000]}\n"
            f"--- FIN DU CONTENU ---\n"
        )
        resp = await call_llm(LLM_MODEL, soul, ctx, max_tokens=2048, timeout=300)
        _trace_llm(shared, "actufinder_synthesize", "exec", LLM_MODEL, soul, ctx, resp)
        try:
            decision = _extract_json(resp)
        except Exception as e:
            log.warning(f"SynthesizeArticle: invalid JSON ({e}), fallback content")
            decision = {}
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            data = {}
        synthesis = data.get("synthesis") or data.get("main_idea") or ""
        has_character = bool(data.get("has_character"))

        article = shared.get("selected_article", {})
        article["synthesis"] = synthesis
        # Le personnage central est déjà garanti par la chaîne amont :
        # llm_select (pydantic: character_name obligatoire) + title_gate
        # (filtre "perso nommé") l'écrivent dans article["character_name"].
        # On dérive simplement le dict character attendu en aval (scriptwriter
        # alt, assetfinder/danbooru) au lieu de re-demander un 3e appel LLM
        # (extract_character était incohérent : parfois null alors que le gate
        # avait identifié le perso -> rebouclage inutile).
        name = str(article.get("character_name") or "").strip()
        franchise = str(article.get("franchise") or "").strip()
        if name:
            article["character"] = {
                "name": name,
                "franchise": franchise,
                "search_terms": f"{name} {franchise} character",
            }
        else:
            article["character"] = {}
        shared["selected_article"] = article
        shared["_current_step"] = "actufinder_synthesize_done"
        shared["steps"].append({
            "step": "actufinder_synthesize", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"synthesis={len(synthesis)} chars, has_character={has_character}, character={name or 'none'}",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        _save_sub_shared("actufinder", shared)
        # Approve si un personnage est connu (garanti par le gate en amont),
        # sinon on reboucle vers un autre flux RSS.
        return "approve" if name else "no_good_news"
