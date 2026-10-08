import json
import logging
import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
from pocketflow import AsyncNode

from config_actufinder import (
    ALL_FEEDS,
    BLOCKED_SOURCES, BLOCKED_URL_HOSTS,
    DECIDER_QUESTIONS, DECIDER_CATEGORY_RANK, DECIDER_MIN_PRIORITY,
    DECIDER_GACHA_BONUS, DECIDER_HOOK_BONUS, DECIDER_TOP_K, DECIDER_MAX_CTX_TOKENS,
    POOL_MAX_ARTICLES,
)
from config import LLM_MODEL
from helpers.state import (
    _set_state, _shared_snapshot, _set_traces, _save_sub_shared,
)

# Longueur max du contenu d'article mémorisé pour le scriptwriter (caractères)
ARTICLE_CONTENT_MAX = 4000
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, LLMJSONQuoteError
from helpers.send_telegram import send_telegram

log = logging.getLogger("pocketflow-pipeline")

# Seuils de score du LLM
GOOD_SCORE_MIN = 75  # en dessous → on considère qu'il n'y a pas de bonne news

# Âge max des articles acceptés (en jours)
MAX_AGE_DAYS = 7


def video_source_of(article: dict) -> str | None:
    """Renvoie l'éditeur vidéo si l'article est vidéo-only, sinon None.

    Deux signaux car `source` peut être vide dans un flux :
    - `source` : l'élément <source> du RSS Google News (= l'éditeur) ;
    - le suffixe du titre : Google News titre les entrées "<titre> - <éditeur>".

    Indispensable car l'URL est une redirection news.google.com/rss/articles/...
    et ne révèle donc jamais l'éditeur réel.
    """
    src = (article.get("source") or "").strip().lower()
    if src in BLOCKED_SOURCES:
        return src
    title = (article.get("title") or "").strip().lower()
    for blocked in BLOCKED_SOURCES:
        if title.endswith(f" - {blocked}"):
            return blocked
    return None


def blocked_host(url: str) -> str | None:
    """Renvoie le host si l'URL EST DÉJÀ RÉSOLUE vers une plateforme vidéo.

    À n'utiliser que sur l'URL réelle (après resolve_short_url) : sur l'URL
    Google News le host est toujours news.google.com et le test ne dit rien.
    """
    if not url:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    return host if host in BLOCKED_URL_HOSTS else None

# Nombre max d'articles présentés au LLM (repli historique, sans decider).
# Le chemin principal passe par DeciderScoreNode -> DECIDER_TOP_K candidats.
MAX_ARTICLES_TO_LLM = 8


class FetchAllFeedsNode(AsyncNode):
    """Télécharge TOUS les feeds RSS en parallèle et merge le pool brut.

    Les 13 feeds (RSS_FEEDS + WAFU_PRIORITY_FEEDS) sont poolés en un seul lot :
    plus de boucle de retry flux par flux. Déduplication par URL de flux
    (un même article présent sur 2 feeds n'est compté qu'une fois, l'ordre du
    premier feed rencontré est conservé).
    """

    def __init__(self):
        super().__init__(max_retries=2, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_fetch_all"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        async def _fetch(url):
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                    r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                return self._parse_feed(r.text)
            except Exception as e:
                log.warning(f"ActuFinder: feed {url} en échec: {e}")
                return []

        results = await asyncio.gather(*(_fetch(u) for u in ALL_FEEDS))
        seen_urls = set()
        merged = []
        for articles in results:
            for a in articles:
                if not a.get("url") or a["url"] in seen_urls:
                    continue
                seen_urls.add(a["url"])
                merged.append(a)
        return {"articles": merged}

    def _parse_feed(self, xml_text: str) -> list:
        articles = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            log.warning(f"ActuFinder: RSS parse error: {e}")
            return []

        def _local(tag: str) -> str:
            return tag.split("}")[-1] if "}" in tag else tag

        for item in root.iter():
            if _local(item.tag) not in ("item", "entry"):
                continue
            entry = {"title": "", "description": "", "url": "", "published_at": "", "source": ""}
            for child in item:
                name = _local(child.tag).lower()
                text = (child.text or "").strip()
                if name == "title":
                    entry["title"] = text
                elif name == "description":
                    entry["description"] = text
                elif name == "link":
                    entry["url"] = child.get("href", "") or text
                elif name in ("pubdate", "published", "updated"):
                    entry["published_at"] = text
                elif name == "source":
                    entry["source"] = text
            if entry["title"]:
                articles.append(entry)
        return articles

    async def post_async(self, shared, prep, exec):
        shared["raw_articles"] = exec["articles"]
        shared["_current_step"] = "actufinder_fetch_all_done"
        shared["steps"].append({
            "step": "actufinder_fetch_all", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(exec['articles'])} articles ({len(ALL_FEEDS)} feeds mergés)",
        })
        await _set_state(**_shared_snapshot(shared))
        _save_sub_shared("actufinder", shared)
        return "default"


class DeciderScoreNode(AsyncNode):
    """Score rapide du pool via decider-4b (in-process, libllama HIP).

    Pour chaque article : 4 questions en un forward (perso féminin nommé ?
    catégorie ? gacha ? hook ?). Un article est ELIGIBLE si perso féminin de
    fiction nommé (Q1=yes) ET prioritÉ de catégorie >= DECIDER_MIN_PRIORITY.
    Score combine = priorité*10 + gacha + hook + fraîcheur. Le pool est
    tronqué aux ~300 premiers (POOL_MAX_ARTICLES), le Top-DECIDER_TOP_K est
    transmis au LLM final.

    - "default" : au moins un candidat éligible
    - "no_good_news" : zéro article éligible -> arrêt propre (plus de boucle
      de retry flux par flux).
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_decider_score"
        await _set_state(**_shared_snapshot(shared))
        return shared

    @staticmethod
    def _free_host_vram():
        """Best-effort : arrête le llama-server local + cleanup (swap) pour
        libérer la VRAM avant de (re)charger decider-4b en in-process. Miroir
        du nœud CleanupLlamaProxy, appelé en secours si le chargement échoue."""
        import time
        import urllib.request

        base = "http://localhost:8080/api/proxy"
        for ep in ("/stop", "/cleanup"):
            try:
                req = urllib.request.Request(
                    base + ep, data=b"", method="POST")
                urllib.request.urlopen(req, timeout=30)
            except Exception:
                pass
        time.sleep(3)

    @staticmethod
    def _freshness(published_at: str) -> int:
        if not published_at:
            return 0
        try:
            dt = parsedate_to_datetime(published_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            return 0
        if age_h < 12:
            return 5
        if age_h < 24:
            return 4
        if age_h < 48:
            return 3
        if age_h < 72:
            return 2
        return 0

    def _context_for(self, a: dict) -> str:
        parts = [
            a.get("title", ""),
            f"Source: {a.get('source', '')}",
            f"Published: {a.get('published_at', '')}",
            (a.get("description") or "")[:300],
        ]
        return "\n".join(p for p in parts if p)

    def _score_articles(self, articles: list):
        """Scoring du pool. Retourne (scored, cached_count) :
        - cached_count = articles dont le verdict venait du cache (0 décoder appelé)
        - scored = liste des articles avec champs ai_*, dans l'ordre du pool."""
        from helpers import decider as D
        from .decider_cache import (
            load_decider_cache, save_decider_cache, cached_verdict_for,
        )

        pool = articles[:POOL_MAX_ARTICLES]
        cache = load_decider_cache()
        cached_count = 0

        # 1) Verdicts déjà connus (URL + titre identique) : réutilisés tels
        #    quels. Seuls les articles NOUVEAUX passent par decider-4b.
        verdicts = {}
        to_score = []
        for i, a in enumerate(pool):
            url = a.get("url", "")
            title = (a.get("title") or "").strip()
            entry = cached_verdict_for(cache, url, title)
            if entry is not None:
                verdicts[url] = entry
                cached_count += 1
            else:
                to_score.append((i, a))

        # 2) Scoring des nouveaux articles (un forward par article, séquentiel).
        #    Preflight moteur : au premier besoin on charge decider-4b (in-process).
        #    En échec → 1 retry après libération VRAM best-effort ; si toujours KO,
        #    UN seul log d'erreur clair et on abandonne le scoring (plus de
        #    warning par article).
        if to_score and not D.engine_loaded():
            try:
                D.engine_load()
            except Exception:
                self._free_host_vram()
                try:
                    D.engine_load()
                except Exception as exc:
                    log.error("ActuFinder: decider-4b impossible à charger malgré "
                              "libération VRAM (%s) — %d article(s) non scoré(s)",
                              exc, len(to_score))
                    to_score = []
        for i, a in to_score:
            ctx = self._context_for(a)
            try:
                decs = D.decide(ctx, DECIDER_QUESTIONS,
                                max_ctx_tokens=DECIDER_MAX_CTX_TOKENS)
            except Exception as e:
                log.warning(f"ActuFinder: decider en échec sur article {i} ({e})")
                continue
            q1_yes = decs[0]["choice"] == "yes"
            category = decs[1]["choice"]
            prio = DECIDER_CATEGORY_RANK.get(category, 0)
            gacha = decs[2]["choice"] == "yes"
            hook = decs[3]["choice"] == "yes"
            eligible = q1_yes and prio >= DECIDER_MIN_PRIORITY
            url = (a.get("url") or "").strip()
            entry = {
                "title": (a.get("title") or "").strip(),
                "q1": q1_yes,
                "cat": category,
                "prio": prio,
                "gacha": gacha,
                "hook": hook,
                "eligible": eligible,
                "conf": round(decs[0]["confidence"], 3),
            }
            cache[url] = entry
            verdicts[url] = entry
        # 3) Persistance (bornée). La fraîcheur n'est JAMAIS cachée : elle est
        #    recalculée à chaque run pour les articles encore frais/septuagés.
        save_decider_cache(cache)

        # 4) Assemblage final dans l'ordre du pool + score complet avec fraîcheur.
        scored = []
        for i, a in enumerate(pool):
            entry = verdicts.get(a.get("url", ""))
            if entry is None:
                continue
            fresh = self._freshness(a.get("published_at", ""))
            score = min(
                100,
                entry["prio"] * 10
                + (DECIDER_GACHA_BONUS if entry["gacha"] else 0)
                + (DECIDER_HOOK_BONUS if entry["hook"] else 0)
                + fresh,
            )
            sub = dict(a)
            sub.update(
                ai_q1=entry["q1"],
                ai_cat=entry["cat"],
                ai_prio=entry["prio"],
                ai_gacha=entry["gacha"],
                ai_hook=entry["hook"],
                ai_conf=entry["conf"],
                ai_eligible=entry["eligible"],
                ai_score=score,
                ai_idx=i,
            )
            scored.append(sub)
        return scored, cached_count

    async def exec_async(self, shared):
        articles = shared.get("filtered_articles", [])
        if not articles:
            return {"scored": 0, "eligible": 0, "candidates": [], "cached": 0}
        scored, cached_count = await asyncio.to_thread(self._score_articles, articles)
        eligible = sorted(
            (s for s in scored if s["ai_eligible"]),
            key=lambda s: (-s["ai_score"], -s["ai_conf"]),
        )
        candidates = eligible[:DECIDER_TOP_K]
        return {
            "scored": len(scored),
            "eligible": len(eligible),
            "candidates": candidates,
            "cached": cached_count,
        }

    async def post_async(self, shared, prep, exec):
        from helpers import decider as D

        shared["decider_candidates"] = exec["candidates"]
        shared["decider_stats"] = {
            "scored": exec.get("scored", 0),
            "eligible": exec.get("eligible", 0),
            "cached": exec.get("cached", 0),
            "pool_max": POOL_MAX_ARTICLES,
            "top_k": DECIDER_TOP_K,
        }
        # Le scoring est terminé : on décharge le modèle pour libérer la VRAM
        # avant l'appel LLM final (et dans tous les cas de sortie).
        D.engine_free()

        shared["_current_step"] = "actufinder_decider_score_done"
        shared["steps"].append({
            "step": "actufinder_decider_score", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(exec['candidates'])} candidat(s) sur "
                      f"{exec.get('eligible', 0)} éligibles / {exec.get('scored', 0)} scorés "
                      f"({exec.get('cached', 0)} du cache)",
        })
        await _set_state(**_shared_snapshot(shared))
        _save_sub_shared("actufinder", shared)

        if not exec["candidates"]:
            try:
                await send_telegram(
                    "❌ ActuFinder : aucun article éligible parmi "
                    f"{exec.get('scored', 0)} analysés. Pipeline arrêté pour ce thème."
                )
            except Exception as e:
                log.warning(f"ActuFinder: telegram abort: {e}")
            return "no_good_news"
        return "default"


class FilterArticlesNode(AsyncNode):
    """Filtre léger avant le LLM : trop vieux, sans titre, doublons, déjà utilisés."""

    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_filter"
        await _set_state(**_shared_snapshot(shared))
        return shared.get("raw_articles", [])

    async def exec_async(self, articles):
        from .used_articles import is_used

        now = datetime.now(timezone.utc)
        seen_urls = set()
        filtered = []
        video_rejected = 0
        for a in articles:
            title = (a.get("title") or "").strip()
            url = (a.get("url") or "").strip()
            if not title:
                continue
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if is_used(url):
                continue
            # Vidéo-only = pas de prose, donc pas d'article extractible plus bas.
            # Coupe ici pour ne pas faire valider sur Telegram un article mort.
            if video_source_of(a):
                video_rejected += 1
                continue
            published = a.get("published_at", "")
            if published:
                try:
                    dt = parsedate_to_datetime(published)
                    if hasattr(dt, "tzinfo") and dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if (now - dt).days > MAX_AGE_DAYS:
                        continue
                except Exception:
                    pass
            filtered.append(a)
        return {"filtered": filtered, "video_rejected": video_rejected}

    async def post_async(self, shared, prep, exec):
        shared["filtered_articles"] = exec["filtered"]
        shared["_current_step"] = "actufinder_filter_done"
        rejected = exec.get("video_rejected", 0)
        out = f"{len(exec['filtered'])} articles (après filtre)"
        if rejected:
            out += f" — {rejected} vidéo(s) exclue(s)"
            log.info("[actufinder] %s vidéo(s) exclue(s) du lot", rejected)
        shared["steps"].append({
            "step": "actufinder_filter", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": out,
        })
        await _set_state(**_shared_snapshot(shared))
        _save_sub_shared("actufinder", shared)
        return "default"


class LLMSelectNode(AsyncNode):
    """Le LLM choisit LA meilleure news à potentiel TikTok parmi les articles filtrés.

    - retourne "default" quand une bonne news est trouvée
    - retourne "no_good_news" sinon (arrêt propre, Top-K épuisé)
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_llm_select"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        from .used_articles import is_used

        # Chemin principal (nouveau) : Top-DECIDER_TOP_K candidats pré-scoreés
        # par decider-4b. Repli historique : premiers articles filtrés (tests,
        # re-sélection après validation sans decider).
        candidates = shared.get("decider_candidates")
        if candidates:
            articles = [c for c in candidates if not is_used(c.get("url", ""))]
        else:
            articles = shared.get("filtered_articles", [])
            articles = [a for a in articles if not is_used(a.get("url", ""))][:MAX_ARTICLES_TO_LLM]
        if not articles:
            return {"status": "no_good_news"}

        to_llm = articles
        lines = []
        for i, a in enumerate(to_llm, 1):
            hint = ""
            if "ai_score" in a:
                flags = []
                if a.get("ai_gacha"):
                    flags.append("gacha")
                if a.get("ai_hook"):
                    flags.append("hook")
                cat_short = (a.get("ai_cat") or "")[:32] + ("…" if len(a.get("ai_cat") or "") > 32 else "")
                hint = f" [ai {a['ai_score']} conf {a.get('ai_conf', 0)} · {cat_short}" \
                       + (f" · {','.join(flags)}" if flags else "") + "]"
            lines.append(
                f"[{i}] title: {a.get('title', '')}{hint}\n"
                f"    source: {a.get('source', '')} | published: {a.get('published_at', '')}\n"
                f"    desc: {(a.get('description') or '')[:500]}"
            )
        articles_block = "\n".join(lines)

        soul = load_soul("actufinder")
        reformat_error = shared.get("_reformat_error", "")
        ctx = (
            f"Thème: {shared.get('topic', '')}\n\n"
            f"Articles présélectionnés ({len(to_llm)}), numérotés [1] à [{len(to_llm)}]:\n"
            f"{articles_block}\n\n"
            f"Retourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Format : {{\"selected_article\": <index entier >= 1>, \"score\": <entier 0-100>, "
            f"\"character_name\": \"...\", \"franchise\": \"...\", "
            f"\"reason\": \"...\", \"hook_angle\": \"...\"}}\n"
            f"Une sélection est IMPOSSIBLE sans personnage féminin NOM identifiable : "
            f"si aucun article n'atteint (score >= {GOOD_SCORE_MIN} ET personnage nommé), "
            f"retourne {{\"selected_article\": null}}.\n"
            f"INTERDIT : aucun guillemet double (\") à l'intérieur des valeurs reason et "
            f"hook_angle — utilise des apostrophes simples (') ou aucune citation."
        )
        if reformat_error:
            ctx += (
                f"\n\n--- REFORMAT REQUIRED ---\n"
                f"Rejeté par validation: {reformat_error}\n"
                f"Corrige le format et retourne EXACTEMENT "
                f"{{\"selected_article\": <index entier >= 1>, \"score\": <entier 0-100>, "
                f"\"character_name\": \"...\", \"franchise\": \"...\", "
                f"\"reason\": \"...\", \"hook_angle\": \"...\"}}"
            )
            shared["_reformat_error"] = ""
        resp = await call_llm(LLM_MODEL, soul, ctx, max_tokens=8192, timeout=900)
        _trace_llm(shared, "actufinder_llm_select", "exec", LLM_MODEL, soul, ctx, resp)
        try:
            decision = _extract_json(resp)
        except LLMJSONQuoteError as e:
            log.warning(f"ActuFinder LLM retry: guillemets internes dans le JSON ({str(e)[:120]})")
            return {"status": "reformat", "error": str(e)}

        idx = decision.get("selected_article")
        if not idx or not (1 <= int(idx) <= len(to_llm)):
            return {"status": "no_good_news"}

        score = int(decision.get("score", 0))
        if score < GOOD_SCORE_MIN:
            return {"status": "no_good_news"}

        character_name = str(decision.get("character_name", "") or "").strip()
        if not character_name:
            # Aucun personnage nommé → la news n'est pas exploitable (pas de
            # visuel I2V). On la considère comme une absence de bonne news.
            return {"status": "no_good_news"}

        art = to_llm[int(idx) - 1]
        return {
            "status": "success",
            "article": {
                "title": art.get("title", ""),
                "url": art.get("url", ""),
                "source": art.get("source", ""),
                "published_at": art.get("published_at", ""),
                "score": score,
                "character_name": character_name,
                "franchise": str(decision.get("franchise", "") or "").strip(),
                "reason": decision.get("reason", ""),
                "hook_angle": decision.get("hook_angle", ""),
            },
        }

    async def post_async(self, shared, prep, exec):
        if exec.get("status") == "reformat":
            shared["_reformat_error"] = (
                "Ta réponse contenait des guillemets doubles non échappés dans "
                "reason ou hook_angle, le JSON était donc invalide. Réécris-le en "
                "remplaçant TOUS les guillemets doubles par des apostrophes simples (')."
            )
            shared["_reformat_attempts"] = shared.get("_reformat_attempts", 0) + 1
            shared["_current_step"] = "actufinder_llm_select_reformat"
            shared["steps"].append({
                "step": "actufinder_llm_select", "status": "reformat",
                "ts": datetime.now(timezone.utc).isoformat(),
                "attempt": shared["_reformat_attempts"],
            })
            await _set_state(**_shared_snapshot(shared))
            if shared["_reformat_attempts"] > 3:
                log.warning("ActuFinder: JSON invalide après 3 reformat, on tente un autre flux")
                return "no_good_news"
            return "reformat"

        if exec.get("status") == "success":
            from .used_articles import mark_used
            article = exec["article"]
            mark_used(article.get("url", ""))
            shared["selected_article"] = article
            shared["_current_step"] = "actufinder_done"
            shared["steps"].append({
                "step": "actufinder_llm_select", "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": f"{article.get('title', '')} (score {article.get('score', 0)})",
            })
            await _set_state(**_shared_snapshot(shared))
            await _set_traces(shared.get("_traces", {}))
            _save_sub_shared("actufinder", shared)
            return "default"

        # Aucune bonne news parmi les candidats -> arrêt propre (plus de boucle
        # de retry flux par flux : le pool est déjà exhaustif).
        log.info("ActuFinder: no good news parmi les candidats présélectionnés")
        shared["_current_step"] = "actufinder_no_good_news"
        shared["steps"].append({
            "step": "actufinder_llm_select", "status": "no_good_news",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": "Aucune news suffisamment intéressante",
        })
        await _set_state(**_shared_snapshot(shared))
        try:
            await send_telegram(
                "❌ ActuFinder : aucun candidat retenu (pool analysé, "
                "Top-K épuisé). Pipeline arrêté pour ce thème."
            )
        except Exception as e:
            log.warning(f"ActuFinder: telegram abort: {e}")
        return "no_good_news"


class FetchArticleContentNode(AsyncNode):
    """Récupère le contenu complet de l'article sélectionné via r.jina.ai.

    Résout le lien court Google News (consent wall / SPA contournés) et stocke
    le texte de l'article dans selected_article["description"].

    - "approve" toujours : un échec réseau ne bloque pas le pipeline
      (le script se fera sur titre/scoring, comportement historique).
    """

    JINA_URL = "https://r.jina.ai/"

    def __init__(self):
        super().__init__(max_retries=2, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_fetch_content"
        await _set_state(**_shared_snapshot(shared))
        return shared.get("selected_article", {})

    async def exec_async(self, article):
        url = article.get("url", "")
        if not url:
            return {"content": "", "error": "no url"}
        # P1 : résout le lien court Google News vers l'URL réelle du site source
        # (r.jina.ai est bloqué par Cloudflare sur les liens Google News → 403).
        target = url
        if "news.google.com" in url or "consent.google.com" in url:
            from helpers.browser_fetch import resolve_short_url
            resolved = await asyncio.to_thread(resolve_short_url, url)
            if resolved.get("ok") and resolved.get("url"):
                target = resolved["url"]
        # Ici target est l'URL RÉELLE : seul endroit où l'éditeur est fiable
        # (l'URL d'origine est une redirection news.google.com). Ni jina ni le
        # navigateur ne savent tirer de la prose d'une vidéo -> on coupe avant
        # la requête, et post_async bascule sur le fallback navigateur.
        host = blocked_host(target)
        if host:
            log.info("[actufinder] fetch jina ignoré, cible vidéo : %s", host)
            return {"content": "", "error": f"video_host:{host}", "target": target}
        try:
            async with httpx.AsyncClient(timeout=45, follow_redirects=True) as c:
                r = await c.get(self.JINA_URL + target, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # r.jina.ai bloqué (403/429...) → contenu indisponible, non bloquant.
            # post_async renverra "fetch_failed" pour basculer sur le navigateur.
            return {"content": "", "error": f"jina http {e.response.status_code}"}
        except httpx.TimeoutException as e:
            return {"content": "", "error": f"jina timeout: {e.__class__.__name__}"}
        except httpx.RequestError as e:
            return {"content": "", "error": f"jina request error: {e.__class__.__name__}"}
        body_full = self._extract_markdown(r.text)
        # On sert à la synthèse le corps d'ARTICLE (skipping la nav markdown
        # en tête : logo, menus, catégories) puis on tronque à 4000 chars.
        # r.jina servait la page de navigation GameSpace (43 liens/4000 chars)
        # à la place de l'article -> has_character échouait sur de la nav.
        start = FetchArticleContentNode._find_article_start(body_full)
        body_cut = body_full[start:] if start else body_full
        # La décision "garbage" se fait sur le corps COMPLET (avant troncature) :
        # la tête de page est souvent de la nav markdown (login/premium/cookies)
        # et un article réel ne commence qu'après. Tronquer d'abord jetterait
        # les articles longs (ex: Crunchyroll = 558 mots de prose après 4k de nav).
        return {
            "content": body_cut[:ARTICLE_CONTENT_MAX],
            "content_full": body_full,
            "article_start": start,
            "target": target,
        }

    @staticmethod
    def _extract_markdown(text: str) -> str:
        marker = "Markdown Content:"
        idx = text.find(marker)
        body = text[idx + len(marker):] if idx != -1 else text
        body = body.replace("[Image 1:", "[Image:").replace("](https://", "](link")
        return body.strip()

    # Bloqueurs durs : la page n'est PAS l'article (challenge anti-bot, erreur
    # serveur, "just a moment"...). Rédhibitoires même avec du contenu riche.
    _HARD_BLOCK_MARKERS = (
        "erreur 403",
        "forbidden",
        "404 not found",
        "cette page n'existe pas",
        "page not found",
        "the page you were looking for",
        "cloudflare",
        "verification de securite",
        "just a moment",
        "security check",
        "back to homescreen",
        "you have been blocked",
        "access denied",
        "attention required",
        "are you a robot",
        "verifying you are human",
        "content unavailable",
    )

    # Bannières de consentement / cookies : ne disqualifient QUE si elles sont
    # en tête de page (fenêtre ~1200 premiers chars). En pied de page (footer
    # "privacy policy / cookie policy") elles sont normales sur tout site de
    # news et ne doivent pas faire jeter un article réel.
    _CONSENT_MARKERS = (
        "cookies et technologies similaires",
        "comment google utilise les cookies",
        "we use cookies",
        "cookie policy",
        "politique de confidentialité",
        "privacy policy",
        "this page describes the reasons why google",
        "gestion des cookies",
        "consentement",
        "accepter et continuer",
        "tout accepter",
        "notre site utilise des cookies",
    )

    # Navigation / login / premium : présents sur le header de quasi tous les
    # sites de news. Ne sont PAS des raisons de jeter un contenu riche (>=60
    # mots de prose). Conservés uniquement pour les contenus courts.
    _NAV_MARKERS = (
        "create account",
        "join for free",
        "go premium",
        "sign in",
        "log in",
        "subscribe now",
        "already have an account",
    )

    # Bloqueurs sponsoring / publicité : rédhibitoires comme les hard-block.
    # r.jina.ai sert parfois l'encart sponsorisé (ex: articles "written in
    # partnership with ZenMarket") au lieu du contenu éditorial. has_character
    # échouait sur cette pub car aucun personnage réel nest dedans.
    _SPONSOR_MARKERS = (
        "in partnership with",
        "written in partnership",
        "sponsored content",
        "sponsored post",
        "advertisement",
        "paid promotion",
        "zenpoints",
        "zenmarket",
        "shipping proxy",
        "free shipping on your first order",
        "sign up and gain",
        "grab your zenmarket",
        "win 20,000",
    )

    @staticmethod
    def _strip_markdown(content: str) -> str:
        """Retire les liens/image markdown (y compris imbriqués
        `[![Image](url)](url)`) pour ne garder que le texte de prose."""
        text = content or ""
        for _ in range(5):
            new = re.sub(r"!?\[[^\]\[]*\]\([^)]*\)", "", text)
            if new == text:
                break
            text = new
        return text

    @staticmethod
    def _find_article_start(body: str, window: int = 800) -> int:
        """Renvoie l'indice où le corps réel de l'article commence, en ignorant
        le préambule de navigation markdown (logo, menus, catégories,
        plateformes, liens vers d'autres articles).

        r.jina.ai sert parfois la navigation du site à la place de l'article
        (ex: GameSpace -> menus Games/3DS/PS4/PC + "recent articles"). Servir
        ces premiers 4000 chars faisait échouer has_character alors que le
        texte éditorial était présent plus bas.

        Principe : on découpe le corps en fenêtres de `window` chars ; la
        première fenêtre contenant assez de mots de prose hors markdown-links
        (>=60, même règle que _looks_like_garbage) marque le début de
        l'article. Retourne 0 si aucun corps n'est trouvé (page sans contenu)."""
        if not body:
            return 0
        start = 0
        # Pas = fenêtre/2 (chevauchement) : une fenêtre qui déborde à cheval sur
        # la fin de la nav et le début de l'article ne doit pas rater le corps.
        stride = max(window // 2, 1)
        while start < len(body):
            chunk = body[start:start + window]
            prose = FetchArticleContentNode._strip_markdown(chunk)
            words = [w for w in prose.split() if re.search(r"[a-zà-ÿ]{4,}", w, re.I)]
            if len(words) >= 60:
                return start
            start += stride
        return 0

    @staticmethod
    def _looks_like_garbage(content: str) -> bool:
        """Détecte un contenu de type bannière/consent wall/politique/navigation
        au lieu de l'article réel (ex: resolve_short_url qui renvoie
        policies.google.com, une page de cookies, un header de site avec
        paywall, ou un encart sponsorisé type "written in partnership with
        ZenMarket"). Ces pages cassent has_character en aval.

        Règle : un contenu avec un vrai corps d'article (>=60 mots de prose hors
        liens markdown) n'est PAS du garbage, même si le header/footer du site
        mentionne login/cookies/privacy (cas Crunchyroll : articles réels jetés
        à cause de la nav du site). Seuls les vrais bloqueurs (403/404/
        Cloudflare/security check, sponsor/pub explicite, à n'importe quelle
        position) restent rédhibitoires pour un contenu riche. Les bannières
        consent/cookies et nav login ne disqualifient que les contenus courts
        (<60 mots)."""
        low = (content or "").lower()
        # Un contenu trop court (< 300 chars utiles) n'est pas un article.
        stripped = " ".join(low.split())
        if len(stripped) < 300:
            return True
        # Heuristique "nav shell" : r.jina renvoie une page de navigation
        # (menus/catégories liés par markdown) au lieu du corps de l'article.
        # On compte les caractères de prose hors liens/image markdown + la
        # densité de liens `](.
        prose = FetchArticleContentNode._strip_markdown(content)
        words = [w for w in prose.split() if re.search(r"[a-zà-ÿ]{4,}", w, re.I)]
        # Densité de liens : >8 liens par 1000 chars = page de catégories/menus
        # (ex: GameSpace 43 liens / 4000 chars). Un article réel a très peu de
        # liens markdown par rapport à sa prose.
        link_density = content.count("](") * 1000 / max(len(stripped), 1)
        is_nav_shell = link_density > 8 and len(words) < 80
        if len(words) < 60 or is_nav_shell:
            # Page de navigation pure (nav shell) : rédhibitoire en soi, c'est
            # une page de catégories/menus, pas un article (ex: GameSpace — 43
            # liens/4000 chars, ~34 mots de prose hors liens).
            if is_nav_shell:
                return True
            # Contenu court : bannières consent + nav login + hard blockers +
            # sponsor/ads suffisent à jeter (bannière de cookies, page
            # sécurité, login wall).
            hits = [m for m in FetchArticleContentNode._CONSENT_MARKERS +
                    FetchArticleContentNode._HARD_BLOCK_MARKERS +
                    FetchArticleContentNode._NAV_MARKERS +
                    FetchArticleContentNode._SPONSOR_MARKERS if m in low]
            return bool(hits)
        # Contenu riche (>=60 mots de prose) = un vrai article. Le header/footer
        # du site peut mentionner login/premium/cookies/privacy (cas Crunchyroll)
        # sans que le contenu soit du garbage : seuls les vrais bloqueurs
        # (403/404/Cloudflare/security check, sponsor/pub explicite, à
        # n'importe quelle position) disqualifient.
        return any(m in low for m in FetchArticleContentNode._HARD_BLOCK_MARKERS +
                   FetchArticleContentNode._SPONSOR_MARKERS)

    async def post_async(self, shared, prep, exec):
        content = exec.get("content", "")
        error = exec.get("error", "")
        article = shared.get("selected_article", {})
        step = {
            "step": "actufinder_fetch_content",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if content and self._looks_like_garbage(exec.get("content_full") or content):
            log.info(f"ActuFinder: contenu jina = page politique/cookies/sponsor ({len(content)} chars), bascule navigateur")
            content = ""
            error = f"jina/consent/sponsor page content (article_start={exec.get('article_start')})"

        if content:
            article["description"] = content
            step["status"] = "ok"
            step["output"] = f"{len(content)} chars"
        else:
            step["status"] = "failed"
            step["error"] = error or "no content extracted"
        if "description" not in article and error:
            article["description"] = ""
        shared["selected_article"] = article
        shared["steps"].append(step)
        shared["_current_step"] = "actufinder_fetch_content_done"
        await _set_state(**_shared_snapshot(shared))
        _save_sub_shared("actufinder", shared)
        # Pas de contenu (CAPTCHA / site bloqué via r.jina.ai) → bascule sur le
        # fetch navigateur (browser-harness) qui ouvre la vraie page.
        return "approve" if content else "fetch_failed"


class BrowserFetchArticleNode(AsyncNode):
    """Fallback : récupère le contenu de l'article dans un vrai navigateur.

    Passage 1 : script déterministe via browser-harness (Ungoogled Chromium
    headless, CDP 9222) — détecte le challenge (marqueurs FR/EN/DE) et résout
    un widget reCAPTCHA/Turnstile via CapBypass.
    Passage 2 : si échec, boucle agentique (qwen-opus + function-calling)
    qui pilote browser-harness pour extraire le contenu malgré captchas,
    paywalls ou layouts exotiques.

    Action de sortie :
    - "approve" si du contenu a été extrait → on enchaîne vers le scriptwriter alt.
    - "retry_fetch" si le contenu est introuvable (CAPTCHA structurellement
      irrésoluble, paywall, bloqueur type DataDome/PerimeterX non gérés par
      CapBypass) → l'article est marqué comme utilisé et on reboucle sur
      ActuFinder pour en sélectionner un autre.
    """

    def __init__(self):
        super().__init__(max_retries=2, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_fetch_browser"
        await _set_state(**_shared_snapshot(shared))
        return shared.get("selected_article", {})

    async def exec_async(self, article):
        from helpers.browser_fetch import fetch_article_async, resolve_short_url

        url = article.get("url", "")
        if not url:
            return {"content": "", "error": "no url"}
        # P1 : résout le lien court Google News vers l'URL réelle du site source
        # pour éviter le consent wall + la SPA Google (cause du max steps actuel).
        target = url
        if "news.google.com" in url or "consent.google.com" in url:
            resolved = await asyncio.to_thread(resolve_short_url, url)
            if resolved.get("ok") and resolved.get("url"):
                target = resolved["url"]
        # Dernier rempart : ici on paie une navigation Chromium (+Crédits
        # CapBypass) pour extraire de la prose. Sur une vidéo c'est perdu d'avance,
        # donc on rend la main -> post_async renvoie "retry_fetch" et l'ActuFinder
        # resélectionne un autre article.
        host = blocked_host(target)
        if host:
            log.info("[actufinder] fetch navigateur ignoré, cible vidéo : %s", host)
            return {"content": "", "error": f"video_host:{host}"}
        return await fetch_article_async(target)

    async def post_async(self, shared, prep, exec):
        content = exec.get("content", "")
        error = exec.get("error", "")
        article = shared.get("selected_article", {})
        step = {
            "step": "actufinder_fetch_browser",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if content and FetchArticleContentNode._looks_like_garbage(content):
            log.info(f"ActuFinder: contenu navigateur = page politique/cookies ({len(content)} chars), requalifié échec")
            content = ""
            error = "cookie/consent page content"

        if content:
            article["description"] = content
            step["status"] = "ok"
            step["output"] = f"{len(content)} chars (browser)"
        else:
            step["status"] = "failed"
            step["error"] = error or "no content extracted"
        if "description" not in article and error:
            article["description"] = ""
        shared["selected_article"] = article
        shared["steps"].append(step)
        shared["_current_step"] = "actufinder_fetch_browser_done"
        await _set_state(**_shared_snapshot(shared))
        _save_sub_shared("actufinder", shared)
        if content:
            return "approve"
        # Aucune méthode n'a pu extraire l'article (CAPTCHA non résoluble,
        # paywall, bloqueur). On l'exclut et on re-sélectionne un autre article
        # de la MÊME liste filtrée (llm) au lieu de repiocher un flux RSS
        # complet — les flux déjà tentés ne sont pas gaspillés à chaque échec.
        url = article.get("url", "")
        if url:
            from .used_articles import mark_used
            mark_used(url)
            log.info(f"ActuFinder: article injoignable, exclu et retry -> {url[:80]}")
        filtered = shared.get("filtered_articles", [])
        shared["filtered_articles"] = [a for a in filtered if a.get("url") != url]
        return "retry_fetch"
