import json
import logging
import random
import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, unquote

import httpx
from pocketflow import AsyncNode

from config_actufinder import (
    RSS_FEEDS, WAFU_PRIORITY_FEEDS, BLOCKED_SOURCES, BLOCKED_URL_HOSTS,
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

# Nombre max d'articles présentés au LLM (les plus récents en priorité).
# Modèle de raisonnement (qwen3.6q2) = verbeux : un prompt trop gros fait
# dépasser le timeout (ReadTimeout 600s). On borne à 8 pour raisonner vite.
MAX_ARTICLES_TO_LLM = 8

# Nombre de tentatives max (flux RSS différents) avant d'abandonner.
# Ne compte QUE les vraies "no good news" (articles présents mais jugés
# insuffisants) ; un feed VIDE (0 article) ne consomme pas une tentative.
MAX_RETRIES = 15


class RandomFeedNode(AsyncNode):
    """Choisit un flux RSS au hasard et gère le compteur de tentatives du subflow.

    - retourne "default" quand un flux est choisi
    - retourne "abort" après MAX_RETRIES sans bonne news (boucle no_good_news)
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_random_feed"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        # Un feed vide (0 article) est un problème ponctuel de feed/actu et ne
        # consomme PAS une tentative d'abandon : on retente un autre flux sans
        # compter. Seule une vraie absence de bonne news (articles présents mais
        # jugés insuffisants par le LLM) incrémente le compteur.
        empty_feed = bool(shared.pop("_actufinder_empty_feed", False))
        attempts = shared.get("_actufinder_attempts", 0)
        if not empty_feed:
            attempts += 1
            shared["_actufinder_attempts"] = attempts
        if attempts > MAX_RETRIES:
            log.info(f"ActuFinder: max retries ({MAX_RETRIES}) atteint, stop")
            return {"abort": True}

        # Échantillonnage SANS remise : chaque tentative pioche un feed non encore
        # tenté dans ce run, pour maximiser la diversité (au lieu de re-tomber sur
        # les mêmes feeds pauvres). Les feeds ciblés "personnage féminin" (waifu)
        # sont tirés EN PRIORITÉ : on ne se rabat sur les feeds génériques qu'une
        # fois que tous les prioritaires ont été tentés. Si tous les feeds ont été
        # vus, on repart du début.
        tried = shared.setdefault("_actufinder_tried_feeds", [])
        prio = [f for f in WAFU_PRIORITY_FEEDS if f not in tried]
        if prio:
            candidates = prio
        else:
            candidates = [f for f in RSS_FEEDS if f not in tried]
            if not candidates:
                tried.clear()
                candidates = list(RSS_FEEDS)
        url = random.choice(candidates)
        tried.append(url)
        query = urlparse(url).query
        q = ""
        for part in query.split("&"):
            if part.startswith("q="):
                q = unquote(part[2:])
                break
        log.info(f"ActuFinder: feed selected -> '{q}' (tentative {attempts})")
        return {"query": q, "url": url, "attempt": attempts}

    async def post_async(self, shared, prep, exec):
        if exec.get("abort"):
            shared["_current_step"] = "actufinder_no_feed"
            shared["steps"].append({
                "step": "actufinder_random_feed", "status": "abort",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": f"max retries ({MAX_RETRIES}) atteint",
            })
            shared["_actufinder_aborted"] = True
            await _set_state(**_shared_snapshot(shared))
            await send_telegram(
                "❌ ActuFinder : aucune bonne news trouvée après "
                f"{MAX_RETRIES} tentatives. Pipeline arrêté pour ce thème."
            )
            _save_sub_shared("actufinder", shared)
            return "abort"

        shared["selected_feed"] = exec
        shared["_current_step"] = "actufinder_random_feed_done"
        shared["steps"].append({
            "step": "actufinder_random_feed", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": "", "output": exec["query"],
        })
        await _set_state(**_shared_snapshot(shared))
        _save_sub_shared("actufinder", shared)
        return "default"


class FetchRSSNode(AsyncNode):
    """Télécharge le flux RSS sélectionné et recupere les articles bruts."""

    def __init__(self):
        super().__init__(max_retries=2, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_fetch"
        await _set_state(**_shared_snapshot(shared))
        return shared.get("selected_feed", {})

    async def exec_async(self, feed):
        url = feed.get("url", "")
        if not url:
            raise RuntimeError("ActuFinder fetch: no feed url")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
        return {"articles": self._parse_feed(r.text)}

    def _parse_feed(self, xml_text: str) -> list:
        articles = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            log.warning(f"ActuFinder: RSS parse error: {e}")
            return []

        def _local(tag: str) -> str:
            return tag.split("}")[-1] if "}" in tag else tag

        # Google News renvoie un flux Atom (<entry>), on couvre item/entry.
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
        shared["_current_step"] = "actufinder_fetch_done"
        shared["steps"].append({
            "step": "actufinder_fetch", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(exec['articles'])} articles",
        })
        await _set_state(**_shared_snapshot(shared))
        _save_sub_shared("actufinder", shared)
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
    - retourne "no_good_news" sinon (boucle de retry vers RandomFeedNode)
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "actufinder_llm_select"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        from .used_articles import is_used

        articles = shared.get("filtered_articles", [])
        if not articles:
            return {"status": "no_good_news", "empty": True}

        # On exclut les URLs déjà consommées dans CE run (rejet title_gate,
        # extract_character, validation feedback...) pour éviter de re-soumettre
        # au LLM un article déjà écarté.
        articles = [a for a in articles if not is_used(a.get("url", ""))]
        if not articles:
            return {"status": "no_good_news"}

        to_llm = articles[:MAX_ARTICLES_TO_LLM]
        lines = []
        for i, a in enumerate(to_llm, 1):
            lines.append(
                f"[{i}] title: {a.get('title', '')}\n"
                f"    source: {a.get('source', '')} | published: {a.get('published_at', '')}\n"
                f"    desc: {(a.get('description') or '')[:500]}"
            )
        articles_block = "\n".join(lines)

        soul = load_soul("actufinder")
        reformat_error = shared.get("_reformat_error", "")
        ctx = (
            f"Thème: {shared.get('topic', '')}\n\n"
            f"Articles du flux sélectionné ({len(to_llm)}), numérotés [1] à [{len(to_llm)}]:\n"
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

        # Aucune bonne news → route vers RandomFeedNode pour choisir un autre flux.
        # On distingue un feed VIDE (0 article : problème réseau/actu, ne consomme
        # pas une tentative d'abandon) d'une vraie absence de bonne news.
        shared["_actufinder_empty_feed"] = bool(exec.get("empty"))
        log.info(
            f"ActuFinder: no good news (empty_feed={shared['_actufinder_empty_feed']})"
        )
        shared["_current_step"] = "actufinder_no_good_news"
        shared["steps"].append({
            "step": "actufinder_llm_select", "status": "no_good_news",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": "Aucune news suffisamment intéressante",
        })
        await _set_state(**_shared_snapshot(shared))
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
