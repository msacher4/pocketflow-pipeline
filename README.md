# PocketFlow Pipeline

Pipeline de génération automatique de vidéos IA : il détecte une actu "waifu" (personnage féminin d'anime/jeu), écrit un script court, génère les assets visuels (I2V/T2V), la musique, la voix off, puis assemble le montage final.

> État : **base stable v1** — run de production validé. Les briques sont en place, les patchs se font par commits git au fil de l'eau.

## Architecture / flux

Le daemon tourne en boucle (intervalle configurable) et choisit à chaque cycle entre deux chemins :

```
router
  ├─ normal  → viralfinder → video_analysis → scriptwriter          (validation Telegram par étape)
  └─ alt     → actufinder  → scriptwriter alt                       (chemin actif actuellement)
```

- **ActuFinder** : pioche un flux Google News RSS "personnage féminin", filtre les sources vidéo-only bloquées (YouTube, TikTok…), synthétise un article, vérifie la présence d'un personnage.
- **ScriptWriter (alt)** : écrit le script de 3-8 plans (`### HOOK / ##1 BODY / ##2 OUTRO`), VO par plan, avec validation LLM (cohérence VO + structure pydantic) et **auto-repair mécanique** des erreurs d'horaires (sans rappel LLM). Approbation via boutons Telegram.
- **AssetPlanner (alt)** : découpe le script en slots. Les 1ʳᵉ `Video:` des Plans 1 et 2 sont marquées **I2V** (image réelle du personnage → vidéo), les autres en **T2V**. Garantie **max 2 vidéos par plan**.
- **SDCpp VideoGenerator (T2V) / AltI2V (I2V)** : génération vidéo via stable-diffusion.cpp (LTX 2.5, spec 97 frames @24fps, base 320×576 → upscale ×2 = 640×1152), garde-fou "headcount" sur les prompts (1 personne max).
- **Musique / Voix / Montage** : ACE-Step (TTS) pour la VO, xtts/acestep pour la musique (durée cible calculée depuis le nombre de mots de la VO), montage ffmpeg avec `apad` (bonus : la sortie n'est plus tronquée par la musique plus courte).

## Structure du projet

```
pocketflow_pipeline/
├── main.py                 # daemon FastAPI (port 8766) + API de debug/état
├── config.py               # config globale (LLM, SDCPP, ACESTEP…) + secrets via .env
├── config_actufinder.py    # config ActuFinder (RSS, browser-harness, CapBypass)
├── .env                    # secrets locaux — NE PAS COMMITTER (voir .env.example)
├── souls/                  # "âmes" LLM : prompts système par nœud (script_generator_alt.md, etc.)
├── knowledge/              # connaissances annexes (structures waifu)
├── nodes/
│   ├── actufinder/         # source RSS, synthèse article, détection personnage
│   ├── scriptwriter/       # générateur de script + validation pydantic + auto-repair timing
│   ├── assetfinder/        # planification assets, T2V/I2V, musique, voix, upscale
│   ├── videoeditor/        # montage final
│   ├── validation/         # nœuds d'approbation/feedback Telegram
│   ├── video_analysis/     # analyse vidéo (chemin normal)
│   └── viralfinder/        # sélection actu brute (chemin normal)
├── helpers/                # API externes (sdcpp, comfyui, acestep, llama, telegram, headcount_guard…)
├── mcp/                    # serveurs MCP (ex. TikHub)
├── telegram_poller/        # poller des callbacks/boutons Telegram
├── ui/                     # vue Svelte de supervision (node_modules/dist ignorés)
├── tests/                  # scripts de diagnostic T2V/I2V habillage (lourds ignorés)
├── downloads/ output/      # runtime généré — ignorés par git
└── archive_src/            # anciens workers (référence)
```

## Prérequis

- **Python 3.12** (`/usr/bin/python3`) — le daemon tourne sur le python système.
- Paquets Python (site-packages **user**, `~/.local` ) : `fastapi`, `uvicorn`, `aiohttp`, `httpx`, `pydantic`, `python-dotenv`, `faster-whisper`, `PIL`, `ddgs`, `youtubesearchpython`.
- **Service systemd user** (ceux qui doivent tourner) :
  - `pocketflow-pipeline.service` — le daemon (ExecStart `python3 main.py`)
  - `llama-proxy.service` — proxy local LM (auto-switch) sur `127.0.0.1:8080`
  - `tg-callback-poller.service` — poller Telegram (callbacks + feedback)
- **Outils externes** : `ffmpeg`, binaire `sd-cli` (stable-diffusion.cpp build `ltx25`), modèles GGUF LTX 2.5, ComfyUI (upscale), ACE-Step (TTS/synth), modèle Whisper, gestionnaire GPU (Rocm).
- Disque ext4 sur `/media/marcs/Linux_Apps` (règles du projet : jamais sur le disque système).

## Installation

```bash
cd /media/marcs/Linux_Apps/Projets_AI/pocketflow_pipeline

# 1. dépendances Python (le python système est verrouillé par PEP 668)
/usr/bin/python3 -m pip install --user --break-system-packages python-dotenv

# 2. secrets : créer le .env local à partir du template
cp .env.example .env
# puis remplir avec les vraies valeurs (voir section ci-dessous)
```

## Lancement

```bash
# dépendances d'abord
systemctl --user start llama-proxy tg-callback-poller

# le pipeline
systemctl --user start pocketflow-pipeline
systemctl --user status pocketflow-pipeline

# état et API
curl http://127.0.0.1:8766/api/state        # courant, step en cours
curl http://127.0.0.1:8766/api/state/sub/<step>   # état d'un step
curl http://127.0.0.1:8766/api/flow          # graphe du flow
```

Le daemon tourne en boucle : un run toutes les `PF_SCHEDULE_INTERVAL` heures (défaut 4 h). Pour un run manuel : `POST /api/state/trigger`. Arrêt : `systemctl --user stop pocketflow-pipeline`.

> Redémarrage après une modif de code : `systemctl --user restart pocketflow-pipeline`. Le daemon repart avec `Restart=always`.

## Variables d'environnement (secrets)

Toutes sont chargées depuis `.env` via `python-dotenv` (`load_dotenv()` en tête de `config.py` / `config_actufinder.py`). Le service systemd fournit aussi `PF_DAEMON_PORT`, `PF_SCHEDULE_INTERVAL`, `PF_TG_BOT_TOKEN`, `OPENROUTER_API_KEY` (voir `systemctl --user cat pocketflow-pipeline`).

| Variable | Description |
|---|---|
| `PF_AGENT_TOKEN_SCRIPTWRITER` | Token ZeroClaw agent scriptwriter |
| `PF_AGENT_TOKEN_ASSETFINDER` | Token ZeroClaw agent assetfinder |
| `PF_OPENCODE_API_KEY` | Clé API OpenCode Go (GLM 5.3 Flash — scriptwriter alt) |
| `TIKHUB_API_KEY` | Clé API TikHub (vidéos/réseaux sociaux) |
| `CAPBYPASS_API_KEY` | Clé CapBypass (résolution CAPTCHA, secours fetch) |
| `PF_TG_BOT_TOKEN` | Token bot Telegram (validations) |
| `OPENROUTER_API_KEY` | Clé OpenRouter (décisions JEV / headcount) |

⚠️ **Ne jamais commiter le `.env` rempli.** Les autres réglages non-sensibles (`PF_SDCPP_*`, `PF_ACESTEP_*`, chemins, modèles LLM…) ont des valeurs par défaut en dur dans `config.py` et restent optionnels.

## Git / workflow de patchs

```bash
git pull                       # récupérer les changements
# ... modifier le code ...
git add -A                     # ou fichiers ciblés
git commit -m "fix: description du patch"
git push
```

Règles : un commit par sujet, messages clairs, secrets uniquement dans `.env` local (jamais en dur dans le code).