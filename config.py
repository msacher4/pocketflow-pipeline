import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    load_dotenv = None

def _env(name, default=""):
    if load_dotenv is None:
        return os.getenv(name, default)
    return os.getenv(name, default)

# Agent config (transitional — scriptwriter may still call external agent)
AGENT_PORTS = {
    "scriptwriter": 42693,
    "assetfinder": 42682,
}

AGENT_TOKENS = {
    "scriptwriter": _env("PF_AGENT_TOKEN_SCRIPTWRITER", ""),
    "assetfinder": _env("PF_AGENT_TOKEN_ASSETFINDER", ""),
}

AGENT_URLS = {k: f"http://127.0.0.1:{v}/webhook" for k, v in AGENT_PORTS.items()}

SCHEDULE_INTERVAL_HOURS = int(os.getenv("PF_SCHEDULE_INTERVAL", "4"))
DAEMON_PORT = int(os.getenv("PF_DAEMON_PORT", "8766"))

TG_BOT_TOKEN = os.getenv("PF_TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("PF_TG_CHAT_ID", "1155339708")
TG_VALIDATION_TIMEOUT = int(os.getenv("PF_TG_TIMEOUT", "600"))

# Transitional: services to restart at pipeline start
# zeroclaw-assetfinder retiré (ne tourne plus en daemon)
# zeroclaw-videoeditor retiré (subflow PocketFlow natif)
AGENT_SERVICES = [
    "zeroclaw-scriptwriter",
]

AGENTS = ["scriptwriter", "assetfinder"]

# LLM Manager config
from pathlib import Path
LLM_MODEL = os.getenv("PF_LLM_MODEL", "qwen-opus")
LLM_URL = os.getenv("PF_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LLM_ANALYST_MODEL = os.getenv("PF_LLM_ANALYST", "qwen-opus")
LLM_VISION_MODEL = os.getenv("PF_LLM_VISION", "gemma4-12b")
LLM_REWRITE_MODEL = os.getenv("PF_LLM_REWRITE_MODEL", "qwen3.6q2")
LLM_SCRIPTWRITER_MODEL = os.getenv("PF_LLM_SCRIPTWRITER_MODEL", "qwen3.6q2")
LLM_SCRIPTWRITER_ALT_MODEL = os.getenv("PF_LLM_SCRIPTWRITER_ALT_MODEL", "qwen3.8-27b")
# SW InfoMissedGen (alt) route vers l'API OpenCode Go (GLM 5.3 Flash) au lieu du proxy local
OPENCODE_API_KEY = _env("PF_OPENCODE_API_KEY", "")
OPENCODE_LLM_URL = os.getenv("PF_OPENCODE_URL", "https://opencode.ai/zen/go/v1/chat/completions")
OPENCODE_SESSION = os.getenv("PF_OPENCODE_SESSION", "pocketflow-sg")
LLM_SCRIPTWRITER_ALT_REMOTE_MODEL = os.getenv("PF_LLM_SCRIPTWRITER_ALT_REMOTE_MODEL", "glm-5.3-flash")
SOULS_DIR = Path(__file__).parent / "souls"
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# TikHub API
TIKHUB_API_KEY = _env("TIKHUB_API_KEY", "")
TIKHUB_API_BASE = "https://api.tikhub.io"

# OpenRouter (JEV decisions — garde-fou "headcount" sur les prompts vidéo)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODEL = "typesafe/jev-1.13"

# stable-diffusion.cpp (T2V video generation) — LTX 2.5 (build ltx25, worktree dédié)
SDCPP_BIN = os.getenv("PF_SDCPP_BIN", "/media/marcs/Linux_Apps/stable-diffusion.cpp_ltx25/build_ltx25/bin/sd-cli")
# Modèles LTX 2.5 (T2V) — le with-proj est inclus dans le GGUF gemma, pas de connectors.
SDCPP_LTX25_DIR = os.getenv("PF_SDCPP_LTX25_DIR", "/media/marcs/Linux_Apps/LLM/LTX2.5")
SDCPP_LTX25_DIFFUSION = os.getenv("PF_SDCPP_LTX25_DIFFUSION", f"{SDCPP_LTX25_DIR}/ltx-2.5-22b-distilled-transformer-Q4_0.gguf")
SDCPP_LTX25_VAE = os.getenv("PF_SDCPP_LTX25_VAE", f"{SDCPP_LTX25_DIR}/Vae/ltx-2.5-video-vae-conv-bf16.safetensors")
SDCPP_LTX25_LLM = os.getenv("PF_SDCPP_LTX25_LLM", f"{SDCPP_LTX25_DIR}/Encoder/gemma4-12b-with-proj-ltx-2.5-Q4_K_M.gguf")
SDCPP_LTX25_AUDIO_VAE = os.getenv("PF_SDCPP_LTX25_AUDIO_VAE", f"{SDCPP_LTX25_DIR}/Vae/ltx-2.5-audio-vae-bf16.safetensors")
# Upscaler latent LTX-2.5 : x2 en espace latent, puis refine en 4 steps (2e passe).
SDCPP_HIRES_DIR = os.getenv("PF_SDCPP_HIRES_DIR", f"{SDCPP_LTX25_DIR}/Latent Upscale")
SDCPP_HIRES_MODEL = os.getenv("PF_SDCPP_HIRES_MODEL", "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0")
# i2v : même build que le T2V (build_ltx25) — le même binaire gère T2V et I2V (patch
# release gemma déjà compilé, -i/--strength exposés). Modèles LTX 2.5 (with-proj interne).
SDCPP_BIN_I2V = os.getenv("PF_SDCPP_BIN_I2V", "/media/marcs/Linux_Apps/stable-diffusion.cpp_ltx25/build_ltx25/bin/sd-cli")
SDCPP_ROCM_LIB = os.getenv("PF_SDCPP_ROCM_LIB", "/opt/rocm-6.4.0/lib")
SDCPP_FRAMES = int(os.getenv("PF_SDCPP_FRAMES", "97"))          # @24 fps -> 4.04 s (montage "4s chacun")
SDCPP_FPS = int(os.getenv("PF_SDCPP_FPS", "24"))
# Résolution de la passe BASE. Le hires latent x2 produit la sortie finale (640x1152).
# validated sword_640x1152_r4_seed1234567.webm
SDCPP_WIDTH = int(os.getenv("PF_SDCPP_WIDTH", "320"))           # base 9:16 -> hires x2 = 640
SDCPP_HEIGHT = int(os.getenv("PF_SDCPP_HEIGHT", "576"))         # base 9:16 -> hires x2 = 1152
SDCPP_STEPS = int(os.getenv("PF_SDCPP_STEPS", "8"))
SDCPP_T2V_CFG = float(os.getenv("PF_SDCPP_T2V_CFG", "1.0"))    # t2v LTX 2.5 : CFG 1.0 distilled (3.0 causait image fixe + zoom)
SDCPP_T2V_GUIDANCE = float(os.getenv("PF_SDCPP_T2V_GUIDANCE", "3.5"))  # t2v LTX 2.5 : guidance distilled, explicite (avant : defaut sd-cli)
SDCPP_T2V_SAMPLER = os.getenv("PF_SDCPP_T2V_SAMPLER", "euler_a")   # euler_a = ancestral ; euler simple rendait un zoom fige
SDCPP_T2V_HIRES_STEPS = int(os.getenv("PF_SDCPP_T2V_HIRES_STEPS", "3"))  # refine hires ; 4 surchargait la scene
SDCPP_TILE_FRAMES = int(os.getenv("PF_SDCPP_TILE_FRAMES", "2"))  # tile temporel VAE decode (2 = validé ; 4 OOM en hires)
SDCPP_TIMEOUT = int(os.getenv("PF_SDCPP_TIMEOUT", "1800"))     # secondes, kill si dépassé (~10 min mesuré)
SDCPP_I2V_WIDTH = int(os.getenv("PF_SDCPP_I2V_WIDTH", "320"))      # i2v : base alignée sur le T2V -> hires latent x2 = 640
SDCPP_I2V_HEIGHT = int(os.getenv("PF_SDCPP_I2V_HEIGHT", "576"))   # idem T2V -> sortie finale 640x1152 (identique au T2V)
SDCPP_I2V_FRAMES = int(os.getenv("PF_SDCPP_I2V_FRAMES", "97"))     # 97 @ 24 fps = 4.04 s (validé visuellement ; 49 non testé en 640x1152)
SDCPP_I2V_STEPS = int(os.getenv("PF_SDCPP_I2V_STEPS", "8"))         # i2v LTX 2.5 : distill = 8 étapes (validé A/B)
SDCPP_I2V_CFG = float(os.getenv("PF_SDCPP_I2V_CFG", "1.0"))          # i2v LTX 2.5 : CFG 1.0 + guidance distilled 3.5 (validé)
SDCPP_I2V_GUIDANCE = float(os.getenv("PF_SDCPP_I2V_GUIDANCE", "3.5"))    # guidance distilled (1.5 testé : aucun gain visible)
SDCPP_I2V_STRENGTH = float(os.getenv("PF_SDCPP_I2V_STRENGTH", "0.7"))    # 0.7 validé ; 0.4 également validé (plus faithful à la source)
SDCPP_I2V_SAMPLER = os.getenv("PF_SDCPP_I2V_SAMPLER", "euler_a")    # euler_a = ancestral, validé sur les runs 97f 640x1152
SDCPP_I2V_TILE_FRAMES = int(os.getenv("PF_SDCPP_I2V_TILE_FRAMES", "2"))   # tuile temporelle du refine. 4 ne touche QUE le decode VAE
                                                        # (923->906 s) et rate son budget : le retry auto rebascule en 3.
                                                        # 2 reste sous le seuil, donc pas de tentative ratée.
# overlap = 1, comme le défaut de ggml (common.cpp:1059). NE PAS revenir à 0.
# Avec overlap=0, ltx_vae.hpp:1137 avance les tuiles de (window - overlap) = 2 frames
# sans aucun recouvrement : chaque frame est décodée isolément puis collée à sa voisine.
# Résultat : couture temporelle toutes les 2 frames, soit un clignotement visible du
# sujet par fractions de seconde, répété sur toute la durée (symptôme observé et validé
# visuellement). À overlap=1 chaque frame est décodée deux fois comme contexte de la
# suivante ; le rendu est propre. Vérifié en 512x384 ET 640x1152, sans OOM (refine 654 s).
SDCPP_I2V_TILE_OVERLAP = int(os.getenv("PF_SDCPP_I2V_TILE_OVERLAP", "1"))
# Tuile spatiale du refine hires. 128 (comme le T2V) OOM en I2V sur
# "need 6194 Mo / available 1530 Mo". 64 fait rentrer le pic.
SDCPP_I2V_HIRES_TILE = int(os.getenv("PF_SDCPP_I2V_HIRES_TILE", "64"))
# Épingler le DiT sur disque (ResidencyMode::Disk) au lieu de la VRAM : les poids
# deviennent évictables et sont paginés à la demande (ggml_extend_backend.cpp:50).
# Indispensable : SANS ÇA le refine hires 640x1152 échoue
# ("need 6194 Mo / available 1530 Mo") alors que la passe base passe (107 s).
# Le patch 85626f4 libère déjà le DiT avant le DECODE ; il ne le faisait pas
# avant le REFINE, qui est le vrai goulot. Text encoder libéré de la RAM avant (7921 Mo).
SDCPP_I2V_DIFFUSION_PARAMS = os.getenv("PF_SDCPP_I2V_DIFFUSION_PARAMS", "disk")   # "ROCm0" pour épingler en VRAM (échoue en hires)
SDCPP_I2V_DISABLE_PREFETCH = os.getenv("PF_SDCPP_I2V_DISABLE_PREFETCH", "1") == "1"  # 1 = passe --disable-prefetch, donc prefetch asynchrone COUPÉ
                                                        # (common.cpp:540, défaut upstream = prefetch actif). C'est ce qui a servi
                                                        # au run validé 640x1152. Mettre 0 pour laisser le prefetch par défaut.
SDCPP_I2V_HIRES_STEPS = int(os.getenv("PF_SDCPP_I2V_HIRES_STEPS", "3"))    # refine hires. 2 OOM (cudaMalloc 146 Mo) : le plan de découpe change
                                                        # avec le nombre d'étapes, pas seulement le nombre de passes.
SDCPP_SKIP_UPSCALE = os.getenv("PF_SDCPP_SKIP_UPSCALE", "0") == "1"  # 720p passthrough : skip RIFE+lanczos (débogage rapide)
SDCPP_UPSCALE_WIDTH = int(os.getenv("PF_SDCPP_UPSCALE_WIDTH", "1080"))  # résolution cible upscale
SDCPP_UPSCALE_HEIGHT = int(os.getenv("PF_SDCPP_UPSCALE_HEIGHT", "1920"))
SDCPP_UPSCALE_FPS = int(os.getenv("PF_SDCPP_UPSCALE_FPS", "30"))       # fps en sortie (min(src*2, fps))

# acestep.cpp (musique ACE-Step 1.5, remplace ComfyUI pour la génération musicale)
ACESTEP_BIN_DIR = os.getenv("PF_ACESTEP_BIN_DIR", "/media/marcs/Linux_Apps/Projets_AI/acestep.cpp/build")
ACESTEP_BIN_LM = os.getenv("PF_ACESTEP_BIN_LM", f"{ACESTEP_BIN_DIR}/ace-lm")
ACESTEP_BIN_SYNTH = os.getenv("PF_ACESTEP_BIN_SYNTH", f"{ACESTEP_BIN_DIR}/ace-synth")
ACESTEP_MODELS_DIR = os.getenv("PF_ACESTEP_MODELS_DIR", "/media/marcs/Linux_Apps/LLM/Acestep v1.5")
ACESTEP_GGML_BACKEND = os.getenv("PF_ACESTEP_GGML_BACKEND", "Vulkan0")
ACESTEP_LM_TIMEOUT = int(os.getenv("PF_ACESTEP_LM_TIMEOUT", "900"))
ACESTEP_SYNTH_TIMEOUT = int(os.getenv("PF_ACESTEP_SYNTH_TIMEOUT", "900"))
ACESTEP_STEPS = int(os.getenv("PF_ACESTEP_STEPS", "8"))
ACESTEP_SHIFT = float(os.getenv("PF_ACESTEP_SHIFT", "3.0"))
ACESTEP_CFG = float(os.getenv("PF_ACESTEP_CFG", "1.0"))

# Fish Speech S2 (voix off) — réglages échantillonnage + post-traitement
# Les valeurs par défaut correspondent au réglage "C" validé en A/B (expressif + pitch -1 demi-ton).
S2_TEMPERATURE = float(os.getenv("PF_S2_TEMPERATURE", "0.9"))
S2_TOP_P = float(os.getenv("PF_S2_TOP_P", "0.9"))
S2_TOP_K = int(os.getenv("PF_S2_TOP_K", "40"))
S2_MAX_NEW_TOKENS = int(os.getenv("PF_S2_MAX_NEW_TOKENS", "1024"))
# Pitch shift en demi-tons (négatif = voix plus grave). 0 = désactivé.
S2_PITCH_SHIFT = float(os.getenv("PF_S2_PITCH_SHIFT", "-1"))

# Sous-titres (Luckiest Guy)
SUBTITLE_FONTS_DIR = os.getenv("PF_SUBTITLE_FONTS_DIR", str(Path(__file__).parent / "ffmpeg" / "fonts"))

# faster-whisper (transcription sous-titres mot-à-mot)
WHISPER_VENV = os.getenv("PF_WHISPER_VENV", str(Path(__file__).parent.parent / "Faster-WHisper" / "bin" / "python"))
WHISPER_MODEL_DIR = os.getenv("PF_WHISPER_MODEL_DIR", "/media/marcs/Linux_Apps/LLM/FAster-WHisper")
WHISPER_MODEL_NAME = os.getenv("PF_WHISPER_MODEL_NAME", "small")
WHISPER_LANGUAGE = os.getenv("PF_WHISPER_LANGUAGE", None)  # None = auto-détection
