# Clé API CapBypass (https://api.capbypass.pro — résolution de CAPTCHA en
# secours du fetch navigateur). Protocole antiCaptcha-compatible (clientKey /
# createTask / getTaskResult). Compte à créditer pour que les tasks aboutissent.
import os as _os

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    _load_dotenv = None

CAPBYPASS_API_KEY = _os.getenv("CAPBYPASS_API_KEY", "")
CAPBYPASS_BASE_URL = "https://api.capbypass.pro"

# Vue headless Ungoogled Chromium pilotée par le skill browser-harness.
BH_BIN = "/media/marcs/Linux_Apps/.local/share/uv/tools/browser-harness/bin/browser-harness"
BH_CDP = "127.0.0.1:9222"
BH_PROFILE = "/media/marcs/Linux_Apps/projets_ai/bh_profile"
BH_TIMEOUT = 120

# Boucle agent de secours (LLM qwen-opus pilote le navigateur via browser-harness).
BH_AGENT_TIMEOUT = 240     # timeout global du heredoc agent (s)
BH_AGENT_STEPS = 12         # nombre max d'appels LLM (tool-calls) par page
BH_AGENT_MODEL = "qwen-opus"

# Domaines vidéo-only : le contenu n'est pas de la prose.
# Ni r.jina.ai ni le fetch navigateur ne peuvent en extraire un article ->
# has_character échoue, la synthèse sort vide et le script part en vrille.
# (Voir run du 2026-09-28 : un "article" YouTube validé par l'utilisateur puis
#  KeyError 'video' 15 min plus tard dans le ScriptFixer.)
# X/Twitter et Reddit n'y sont PAS : ils ont du texte exploitable.
BLOCKED_SOURCES = {"youtube", "tiktok", "instagram", "twitch", "dailymotion"}
BLOCKED_URL_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "instagram.com", "www.instagram.com", "instagr.am",
    "twitch.tv", "www.twitch.tv",
    "dailymotion.com", "www.dailymotion.com", "dai.ly",
}

# Liste des flux Google News RSS utilisés par ActuFinder.
# Un seul flux est traité par run (choisi au hasard).
# NOTE 2026-09-03 : Google News renvoie des feeds vides pour le filtre temporel
# `when:7d` (endpoint /rss/search). On utilise donc `when:24h` (fraîcheur stricte)
# avec des requêtes génériques à forte actu, prouvées non vides. L'URL est encodée
# en %20 pour éviter toute ambiguïté de parsing.
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=anime%20character%20when:24h",
    "https://news.google.com/rss/search?q=female%20character%20when:24h",
    "https://news.google.com/rss/search?q=game%20trailer%20when:24h",
    "https://news.google.com/rss/search?q=cosplay%20when:24h",
    "https://news.google.com/rss/search?q=gacha%20when:24h",
    "https://news.google.com/rss/search?q=Genshin%20Impact%20banner%20when:24h",
    "https://news.google.com/rss/search?q=Honkai%20when:24h",
    "https://news.google.com/rss/search?q=new%20anime%20game%20when:24h",
    "https://news.google.com/rss/search?q=anime%20girl%20when:24h",
    "https://news.google.com/rss/search?q=video%20game%20character%20when:24h",
]

# Sous-ensemble des feeds les plus ciblés "personnage féminin" (waifu).
# RandomFeedNode tire d'abord dans cette liste (choix aléatoire), et ne se
# rabat sur les feeds génériques que quand tous les prioritaires ont été tentés.
# Motivé par le run 2026-09-07 : le tirage aléatoire pur avait sorti 5 feeds
# génériques de suite ("game trailer", "new anime game", "video game character")
# => aucun personnage féminin identifiable => soul bloquant => abort MAX_RETRIES.
WAFU_PRIORITY_FEEDS = [
    "https://news.google.com/rss/search?q=anime%20character%20when:24h",
    "https://news.google.com/rss/search?q=female%20character%20when:24h",
    "https://news.google.com/rss/search?q=cosplay%20when:24h",
    "https://news.google.com/rss/search?q=Boku%20no%20Hero%20Academia%20when:24h",
    "https://news.google.com/rss/search?q=genshin%20impact%20when:24h",
    "https://news.google.com/rss/search?q=honkai%20when:24h",
    "https://news.google.com/rss/search?q=anime%20girl%20when:24h",
]
