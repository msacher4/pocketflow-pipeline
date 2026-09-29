"""Contexte partagé pour les workers créatifs (Hook/Tension/Visual) et le Combiner."""


def build_common_context(shared) -> str:
    """Construit le contexte des workers à partir du shared store."""
    article = shared.get("selected_article", {})
    article_content = (
        article.get("synthesis")
        or article.get("description")
        or article.get("summary")
        or ""
    )
    character = article.get("character") or {}
    thinking = shared.get("thinking_agent", {})
    parts = [
        f"Thème: {shared.get('topic', '')}",
        f"Article source: {article.get('title', '')}",
        f"Source: {article.get('source', '')}",
        f"URL: {article.get('url', '')}",
    ]
    if character.get("name"):
        parts.append(
            f"Personnage central: {character.get('name')} ({character.get('franchise', '')})"
        )
    if thinking:
        parts.append(
            "===== THINKING AGENT (réflexion en amont) =====\n"
            f"Idée de vidéo: {thinking.get('video_idea', '')}\n"
            f"Pourquoi ça marche: {thinking.get('why_it_works', '')}\n"
            f"Public cible: {thinking.get('target_audience', '')}\n"
            f"Angle affiliation: {thinking.get('affiliate_angle', '')}\n"
            f"Insights recherches: {thinking.get('search_insights', '')}\n"
            f"Concept visuel: {thinking.get('visual_concept', '')}\n"
            f"Mécanisme viral: {thinking.get('viral_mechanism', '')}\n"
            f"Enjeu spectateur: {thinking.get('spectator_stake', '')}\n"
            "===== FIN THINKING AGENT ====="
        )
    parts.append("--- CONTENU COMPLET DE L'ARTICLE ---")
    parts.append(article_content[:5000])
    parts.append("--- FIN DU CONTENU ---")
    return "\n".join(parts)