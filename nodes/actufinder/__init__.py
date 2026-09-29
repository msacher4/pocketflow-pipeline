from .actufinder_node import RandomFeedNode, FetchRSSNode, FilterArticlesNode, LLMSelectNode, FetchArticleContentNode, BrowserFetchArticleNode
from .synthesize_article import SynthesizeArticleNode
from .pydantic_validation import PydanticAFValidationNode
from .title_gate import TitleCharacterGateNode

from pocketflow import AsyncFlow

from nodes.base import ValidationSubFlowNode
from nodes.validation import build_af_news_validation_flow


def build_actufinder_flow() -> AsyncFlow:
    random_feed = RandomFeedNode()
    fetch = FetchRSSNode()
    filtrer = FilterArticlesNode()
    llm = LLMSelectNode()
    pv = PydanticAFValidationNode()
    validate_news = ValidationSubFlowNode(
        "validate_af_news", build_af_news_validation_flow,
        inputs=["topic", "selected_article", "_feedback_attempts"],
        outputs=["selected_article", "_feedback_attempts", "news_feedback", "user_feedback"],
    )
    fetch_article = FetchArticleContentNode()
    synthesize = SynthesizeArticleNode()
    title_gate = TitleCharacterGateNode()

    random_feed >> fetch >> filtrer >> llm >> pv >> title_gate
    # Gate OK (perso nommé) -> validation Telegram humaine (pare-feu anti-spam).
    title_gate - "approve" >> validate_news
    # Pas de personnage féminin nommé (titre/angle) -> on change de feed, sans
    # valider côté humain ni brûler un fetch/synthèse. Le gate protège aussi
    # contre les news "controverse/méta-discours" que extract_character aurait
    # rejetées après coup.
    title_gate - "no_good_news" >> random_feed
    # Après approbation Telegram, on enrichit l'article avant de passer au scriptwriter.
    validate_news - "approve" >> fetch_article
    # r.jina.ai a échoué (CAPTCHA / blocage) → fallback navigateur browser-harness.
    browser_fetch = BrowserFetchArticleNode()
    fetch_article - "fetch_failed" >> browser_fetch
    # Article introuvable via le navigateur (CAPTCHA non résoluble, paywall,
    # bloqueur) → il est exclu (mark_used + retiré de la liste filtrée) et on
    # re-sélectionne un autre article de la MÊME liste. Si plus rien de bon
    # dans cette liste, llm rendra "no_good_news" → reboucle vers un autre flux.
    browser_fetch - "retry_fetch" >> llm

    # Une fois le contenu récupéré (r.jina.ai OU navigateur), on le synthétise
    # via LLM (idée principale propre). Le perso est déjà garanti par la chaîne
    # amont (llm_select pydantic + title_gate -> character_name), synthesize
    # dérive le dict character sans appel LLM supplémentaire.
    fetch_article - "approve" >> synthesize
    browser_fetch - "approve" >> synthesize
    # Perso connu -> sortie du subflow "approve" -> scriptwriter alt (l'action
    # "approve" de synthesize n'a pas de successeur : elle termine le flow).
    # Boucle de retry : pas de bonne news → rejoue avec un autre flux RSS au hasard.
    # RandomFeedNode gère le compteur de tentatives et s'arrête après MAX_RETRIES.
    llm - "no_good_news" >> random_feed
    # Aucun personnage identifiable après synthèse → on exclut l'article et on
    # reboucle vers un autre flux RSS (même mécanisme qu'une mauvaise news).
    synthesize - "no_good_news" >> random_feed
    # JSON LLM invalide → reboucle vers le LLM avec un hint de reformat.
    llm - "reformat" >> llm
    # Article non conforme au schéma pydantic → relance la sélection LLM.
    pv - "reformat" >> llm
    # Rejet de la news en validation → relance la sélection LLM (nouvel angle).
    validate_news - "actufinder_llm_select" >> llm

    return AsyncFlow(start=random_feed)
