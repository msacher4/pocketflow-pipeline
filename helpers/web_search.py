"""Recherche web via DuckDuckGo (gratuit, sans API key).

Utilisé par le Thinking Agent pour appuyer sa réflexion sur les articles.
"""
import logging
from typing import Optional

log = logging.getLogger("pocketflow-pipeline")


def web_search(query: str, limit: int = 5) -> list[dict]:
    """Exécute une recherche DuckDuckGo et retourne les résultats.

    Args:
        query: Requête de recherche
        limit: Nombre max de résultats (défaut: 5)

    Returns:
        Liste de dicts {title, snippet, url} ou liste vide en cas d'erreur.
    """
    query = (query or "").strip()
    # Garde-fou: une vraie requête est courte (< 200 chars) et ne contient
    # pas les artefacts du reasoning (phrases de l'instruction, sauts de ligne).
    if not query or len(query) > 200 or "\n" in query:
        log.warning(f"Web search SKIP (query invalide: {len(query)} chars): {query[:80]}...")
        return []
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=limit))
            return [
                {"title": r.get("title", ""), "snippet": r.get("body", ""), "url": r.get("href", "")}
                for r in results
            ]
    except Exception as e:
        log.warning(f"Web search failed for '{query}': {e}")
        return []


def format_search_results(results: list[dict], query: str = "") -> str:
    """Formate les résultats de recherche en texte lisible pour un LLM."""
    if not results:
        return f"Aucun résultat pour: {query}" if query else "Aucun résultat."

    lines = []
    if query:
        lines.append(f"Résultats pour: {query}")
        lines.append("")
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    {r['snippet']}")
        lines.append(f"    {r['url']}")
        lines.append("")
    return "\n".join(lines)
