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
# Historiquement RandomFeedNode les tirait EN PRIORITÉ ; désormais ALL_FEEDS
# (RSS_FEEDS + WAFU_PRIORITY_FEEDS, dédupés par URL) est mergé en un seul pool.
WAFU_PRIORITY_FEEDS = [
    "https://news.google.com/rss/search?q=anime%20character%20when:24h",
    "https://news.google.com/rss/search?q=female%20character%20when:24h",
    "https://news.google.com/rss/search?q=cosplay%20when:24h",
    "https://news.google.com/rss/search?q=Boku%20no%20Hero%20Academia%20when:24h",
    "https://news.google.com/rss/search?q=genshin%20impact%20when:24h",
    "https://news.google.com/rss/search?q=honkai%20when:24h",
    "https://news.google.com/rss/search?q=anime%20girl%20when:24h",
]

ALL_FEEDS = RSS_FEEDS + [f for f in WAFU_PRIORITY_FEEDS if f not in RSS_FEEDS]

# ========================== Decider 4B (scoreur rapide in-process) ==========================
# Modèle de décision typé ("System One") : lit les logits à des positions précises
# via libllama.so (HIP, build local) — PAS de llama-server. Éligibilité + tri
# grossier du pool avant le LLM final (qwen-opus + soul, contrat inchangé).
DECIDER_GGUF = "/media/marcs/Linux_Apps/LLM/decider-4b/decider-4b-v2.1-Q4_K_M.gguf"
DECIDER_N_CTX = 8192
DECIDER_N_GPU_LAYERS = -1
DECIDER_MAX_CTX_TOKENS = 1536

# Pool unique mergé (tous les feeds), filtré, puis cap avant scoring.
POOL_MAX_ARTICLES = 300
# Nombre de candidats les mieux notés transmis au LLM final.
DECIDER_TOP_K = 15

# Rubrique de scoring (EN, exigée par le modèle). Les libellés de Q2 sont la
# seule source de vérité pour la catégorie → DOIVENT matcher les clés de
# DECIDER_CATEGORY_RANK.
DECIDER_QUESTIONS = [
    {
        "question": "Is the main subject of this headline a named female fictional character (from a video game, anime, TV series, movie or book)?",
        "options": ["yes", "no"],
    },
    {
        "question": "Which category does this news belong to?",
        "options": [
            "new female character reveal from a game or anime",
            "trailer or update revealing a detail about an existing female character",
            "controversy or popularity spike of a specific female character",
            "new skin or outfit for an existing female character",
            "male character or cast roster news",
            "game or service news without any character",
            "anime release date or schedule news",
            "technical patch or maintenance news",
            "none of these",
        ],
    },
    {
        "question": "Is this about a gacha franchise (Genshin, Honkai, ZZZ, NIKKE, Blue Archive, Azur Lane, Epic Seven or similar)?",
        "options": ["yes", "no"],
    },
    {
        "question": "Does this headline have a strong hook (surprise, controversy, hype or an emotional angle that draws attention)?",
        "options": ["yes", "no"],
    },
]

# Priorité de chaque catégorie, alignée sur la table de souls/actufinder.md.
DECIDER_CATEGORY_RANK = {
    "new female character reveal from a game or anime": 10,
    "trailer or update revealing a detail about an existing female character": 10,
    "controversy or popularity spike of a specific female character": 9,
    "new skin or outfit for an existing female character": 9,
    "male character or cast roster news": 4,
    "game or service news without any character": 2,
    "anime release date or schedule news": 1,
    "technical patch or maintenance news": 0,
    "none of these": 0,
}

# Seuil de priorité mini pour être éligible (équivaut à la règle bloquante du
# soul : un article ne passe QUE si perso féminin nommé ET priorité >= 8).
DECIDER_MIN_PRIORITY = 8
DECIDER_GACHA_BONUS = 5
DECIDER_HOOK_BONUS = 5
