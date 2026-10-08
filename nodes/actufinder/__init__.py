from .actufinder_node import (
    FetchAllFeedsNode, FilterArticlesNode, DeciderScoreNode, LLMSelectNode,
    FetchArticleContentNode, BrowserFetchArticleNode,
)
from .synthesize_article import SynthesizeArticleNode
from .pydantic_validation import PydanticAFValidationNode
from .title_gate import TitleCharacterGateNode

from pocketflow import AsyncFlow

from nodes.assetfinder.cleanup_llama_proxy import CleanupLlamaProxy
from nodes.base import ValidationSubFlowNode
from nodes.validation import build_af_news_validation_flow


def build_actufinder_flow() -> AsyncFlow:
    """Flow déterministe, pool unique (depuis dépoussiérage ActuFinder 2026-10-06):

    fetch_all → filter → cleanup_llama_proxy → decider_score → llm_select → pv → title_gate → validation
    Le cleanup (arrêt + libération VRAM du llama-server local) est indispensable
    avant decider_score : sans VRAM libre le GGUF decider-4b ne charge pas
    (le moteur est in-process, chargé dans le nœud puis déchargé après le scoring).
    Plus de boucle de retry flux par flux (RandomFeedNode): le pool merge les 13
    feeds, decider-4b note chaque article (éligibilité perso féminin + priorité),
    le LLM final (qwen-opus + soul) choisit parmi le Top-K. Zéro éligible ou
    Top-K épuisé = arrêt propre.
    """
    fetch_all = FetchAllFeedsNode()
    filtrer = FilterArticlesNode()
    cleanup = CleanupLlamaProxy()
    decider = DeciderScoreNode()
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

    fetch_all >> filtrer >> cleanup >> decider >> llm >> pv >> title_gate
    # Gate OK (perso nommé) -> validation Telegram humaine (pare-feu anti-spam).
    title_gate - "approve" >> validate_news
    # Pas de personnage féminin nommé (titre/angle) -> on re-sélectionne parmi
    # les candidats restants (l'article rejeté est marqué used par le gate).
    # Le pool de candidats est borné (DECIDER_TOP_K) : une fois épuisé, llm
    # rend "no_good_news" -> arrêt propre.
    title_gate - "no_good_news" >> llm
    # Après approbation Telegram, on enrichit l'article avant de passer au scriptwriter.
    validate_news - "approve" >> fetch_article
    # r.jina.ai a échoué (CAPTCHA / blocage) → fallback navigateur browser-harness.
    browser_fetch = BrowserFetchArticleNode()
    fetch_article - "fetch_failed" >> browser_fetch
    # Article introuvable via le navigateur (CAPTCHA non résoluble, paywall,
    # bloqueur) → il est exclu (mark_used) et on re-sélectionne parmi les
    # candidats restants. Plus rien de bon -> llm rend "no_good_news" -> arrêt.
    browser_fetch - "retry_fetch" >> llm

    # Une fois le contenu récupéré (r.jina.ai OU navigateur), on le synthétise
    # via LLM (idée principale propre). Le perso est déjà garanti par la chaîne
    # amont (llm_select pydantic + title_gate -> character_name), synthesize
    # dérive le dict character sans appel LLM supplémentaire.
    fetch_article - "approve" >> synthesize
    browser_fetch - "approve" >> synthesize
    # Perso connu -> sortie du subflow "approve" -> scriptwriter alt (l'action
    # "approve" de synthesize n'a pas de successeur : elle termine le flow).
    # Arrêts propres (aucun successeur = fin du flow) : zéro éligible au
    # decider, Top-K épuisé au LLM, synthèse sans perso.
    # JSON LLM invalide → reboucle vers le LLM avec un hint de reformat.
    llm - "reformat" >> llm
    # Article non conforme au schéma pydantic → relance la sélection LLM.
    pv - "reformat" >> llm
    # Rejet de la news en validation → relance la sélection LLM (nouvel angle).
    validate_news - "actufinder_llm_select" >> llm

    return AsyncFlow(start=fetch_all)