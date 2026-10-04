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
- **RealCharacterImage (alt)** : récupère **2 images Danbooru** par slot I2V. Elles ne servent **pas** de frame de départ, mais de références d'identité.
- **ComfyUI Klein Ref (alt)** : génère l'image I2V avec **Klein 4B en guidage par référence** (nœud natif `ReferenceLatent`). Le prompt décrit la scène et le canvas reste vide : on n'anime donc plus une photo Danbooru, mais une image neuve du personnage. Voir la section dédiée plus bas.
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

## Image I2V : Klein 4B en guidage par référence (chemin alt)

Avant, l'image Danbooru téléchargée par `RealCharacterImageNode` servait
directement de frame de départ à l'I2V. Problème : une photo Danbooru est
choisie pour être un bon **portrait**, pas une image animée — la pose et le
cadrage sont figés et le modèle vidéo déforme la frame 0.

Désormais, sur le chemin alt :

```
RealCharacterImage (2 img Danbooru / slot, references d'identité)
  → ComfyUIKleinRefImageGenerator  ← Klein 4B, canvas vide, references injectées
  → RewriteI2VPrompt (LLM vision, voit l'image Klein)
  → ValidateI2V (Telegram : c'est l'image Klein qui est validée)
  → … → AltI2V
```

**Mécanisme ComfyUI.** FLUX.2 Klein 4B unifie génération et édition : il accepte
des images de référence via le nœud natif `ReferenceLatent`
(`ComfyUI/comfy_extras/nodes_edit_model.py`), qui injecte les latents encodés des
références dans le conditioning. **Aucun modèle supplémentaire n'est requis** — pas
d'IPAdapter, pas de `clip_vision`, les 3 poids sont déjà là (`flux-2-klein-4b-Q8_0.gguf`,
`Qwen3-4B-Q8_0.gguf`, `ae.safetensors`).

**Canvas vide + `denoise=1.0`.** C'est le point le plus important. Brancher la
référence comme canvas reviendrait au mode « édition » de ComfyUI, qui conserve la
composition d'origine et donc largely la photo Danbooru. Ici `EmptyLatentImage`
décrit la sortie, le prompt pilote la composition, et les références ne portent que
l'identité. `tests/test_klein_ref_workflow.py` verrouille ce câblage.

**Les références sont attachées au positif ET au négatif** (comme dans le blueprint
officiel `ComfyUI/blueprints/Image Edit (Flux.2 Klein 4B).json`) : les tokens de
référence doivent appartenir aux deux séquences, sinon le CFG casse.

**Deux références par slot**, enchaînées (`ReferenceLatent` accepte le chaining).
Chacune est réduite à 0,75 MP et alignée sur 16 (`ImageScaleToTotalPixels`) pour
borner le nombre de tokens d'attention. Avec une seule référence disponible, la
seconde duplique la première.

**Substitution, pas ajout.** `_find_image_for_slot` privilégie la première entrée
`confirmed` de `generated_images`, et les images Danbooru arrivent avec
`confirmed: True`. Le nœud Klein **remplace** donc l'entrée du slot (et
`confirmed: False`) : sinon l'I2V continuerait d'animer le Danbooru. Les chemins de
référence sont conservés dans `reference_paths`, ce qui permet au nœud Klein de
relancer une génération.

**Portée limitée au chemin alt, volontairement.** Le bouton « ❌ Refaire l'image »
de la validation du montage reste sur `ComfyUIImageGenerator` (Klein txt2img
**sans** référence) : une nouvelle image entièrement différente, sans continuité
d'identité avec le Danbooru. Seule la chaîne `real >> klein_ref >> rewrite >>
validate` utilise le nœud à références. Conséquence : après un regen via ce
bouton, l'entrée du slot n'a plus de `reference_paths` — sans effet ici, la
validation I2V étant déjà passée ; `validate_i2v -"regen_image" >> real` les
retélécharge si besoin.

**Workflow versionné** : `workflows/generate_image_klein_ref.json`. `_load_workflow`
cherche le dépôt en priorité puis retombe sur le dossier externe historique, donc les
14 workflows existants ne bougent pas.

## Config I2V validée (LTX-2.5, 16 Go)

Ces valeurs ont été **validées visuellement** sur le build local de `stable-diffusion.cpp` (`sdcpp-video-memory-control`). Elles sont mesurées, pas supposées : chaque ligne ci-dessous a été obtenue en observant le rendu. Ne pas les changer sans revalider.

Sortie : **640×1152, 97 frames, 24 fps** (4,04 s), sans scintillement. Passe base 320×576 puis hires latent ×2.

| Réglage | Valeur | Pourquoi — et ce qui casse sinon |
|---|---|---|
| `PF_SDCPP_I2V_TILE_OVERLAP` | `1` | **Ne pas revenir à 0.** À 0, `ltx_vae.hpp` avance les tuiles sans recouvrement : chaque frame est décodée isolément puis recollée → couture toutes les 2 frames, visible comme un clignotement sur toute la durée. À 1 le rendu est propre. |
| `PF_SDCPP_I2V_DIFFUSION_PARAMS` | `disk` | Épingle les poids du DiT hors VRAM pour le refine hires. **Sans ça : OOM « need 6194 Mo / available 1530 Mo »**, alors que la passe base passe. |
| `PF_SDCPP_I2V_HIRES_TILE` | `64` | `128` OOM en I2V (« need 6194 Mo »). 64 fait rentrer le pic. |
| `PF_SDCPP_I2V_HIRES_STEPS` | `3` | `2` OOM (`cudaMalloc` 146 Mo) : le plan de découpe change avec le nombre d'étapes, pas seulement le nombre de passes. |
| `PF_SDCPP_I2V_STRENGTH` | `0.7` | `0.4` fonctionne aussi. ⚠️ Une ancienne docstring affirmait « < 1.0 produit un gris uniforme » : **c'est faux**, testé et infirmé. |
| `PF_SDCPP_I2V_SAMPLER` | `euler_a` | Le chemin I2V sortait sur `euler`, valeur jamais testée. |
| `PF_SDCPP_I2V_FRAMES` | `97` | 49 n'a **jamais** été testé en 640×1152. |
| `PF_SDCPP_I2V_TILE_FRAMES` | `2` | `4` ne gagne que 2 % (923→906 s) et rate son budget : le retry automatique rebascule en 3 tout seul. |
| `PF_SDCPP_I2V_GUIDANCE` | `3.5` | Guidance distilled. `1.5` n'apportait aucun gain visible. |
| `PF_SDCPP_I2V_CFG` | `1.0` | LTX-2.5 est un modèle distillé, la guidance remplace le CFG. |
| `PF_SDCPP_I2V_DISABLE_PREFETCH` | `1` | Passe `--disable-prefetch`, donc prefetch asynchrone **coupé** (défaut sd-cli = actif). `1` → booléen Python `True` ; mettre `0` pour laisser le prefetch par défaut. |

Le `--fps` est passé explicitement sur les deux chemins (T2V et I2V) : `sd-cli` a 24 par défaut, donc l'I2V sortait à 24 fps par hasard, ce qui masquait la constante `FPS = 16.0` qui faussait `duration_s` (6,06 s au lieu de 4,04 s — valeur reprise par `montage_planner` pour la timeline).

**Limites connues, non testées :**

- La chaîne **Klein 320×576 → I2V 320×576** n'a toujours pas été exercée de bout en bout sur le chemin alt, maintenant avec image source **générée par référence**. Si la qualité des images de source se dégrade, c'est la première chose à vérifier.
- **Le rendu Klein avec références n'a pas encore été validé visuellement** : le graphe est validé hors ligne (`tests/test_klein_ref_workflow.py`), mais aucune image n'a encore été produite avec `ReferenceLatent`. Premier point à vérifier : si l'identité ne transparaît pas, basculer `EmptyLatentImage` → `EmptyFlux2LatentImage` (le blueprint officiel FLUX.2 utilise le second).
- **VRAM** : le nœud Klein s'exécute alors que le proxy llama (`qwen-opus` / `gemma4-12b`) est encore chargé. En cas d'OOM, insérer le `CleanupLlamaProxy` déjà importé avant Klein — `RewriteI2VPromptNode` sait déjà réveiller la vision.
- **Un seul slot** a été validé. La boucle multi-slots et `post_async` (merge dans `generated_videos`, `return "regen"` sur échec) n'ont pas été exercés.
- Le T2V **n'a pas été revalidé** : il conserve `--hires-steps 4` / tuile 128, sa valeur d'origine.

## Git / workflow de patchs

```bash
git pull                       # récupérer les changements
# ... modifier le code ...
git add -A                     # ou fichiers ciblés
git commit -m "fix: description du patch"
git push
```

Règles : un commit par sujet, messages clairs, secrets uniquement dans `.env` local (jamais en dur dans le code).

### Versions marquées

Chaque version validée est marquée par un tag **annoté** (`git tag -a`), ce qui conserve le message, la date et l'auteur — un tag tout court ne garderait qu'un pointeur.

| Tag | État de l'I2V |
|---|---|
| `v1-base` | 540×960, 49 frames, `--strength 1.0`, pas de passe hires |
| `v2-540x960` | 540×960, idem — **antérieur** à `v2-i2v-640x1152` malgré le préfixe commun, c'est un point de retour |
| `v2-i2v-640x1152` | **640×1152, 97 frames, 24 fps** — version courante, validée visuellement |

### Revenir à une version antérieure

```bash
# 1. consulter une ancienne version sans rien changer
git show v1-base:config.py

# 2. revenir en arrière en conservant l'historique (sur un dépôt partagé)
git revert <commit> ...

# 3. inspecter une ancienne version dans un dossier détaché
git checkout v1-base
```

⚠️ **Privilégier `git revert` plutôt qu'un `reset`.** Un `reset --hard` suivi d'un push réécrit l'historique partagé et casse le clone de quiconque a déjà récupéré la branche.