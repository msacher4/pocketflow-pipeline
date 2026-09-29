import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.audio_cpp_api import acestep_generate_music, _ace_cpp_available
from helpers.comfyui_api import comfyui_generate_song_15

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"

# Durée de la musique : déduite des mots des VO (≈ 2 mots/sec, le même rythme
# que le budget de parole du script) et non plus fixée en dur à 22s. Sans ça,
# mix_audio() applique -shortest et la musique plus courte que la vidéo
# tronque la FIN de la vidéo (perte mesurée : 47s de vidéo pour 22s de musique).
WORDS_PER_SEC = 2.0
MUSIC_MIN_SEC = 15
MUSIC_MAX_SEC = 90
# Marge : la voix générée parle souvent un peu plus vite que 2 mots/sec, et le
# montage ajoute un peu d'air. On vise légèrement au-dessus du strict nécessaire.
MUSIC_MARGIN_RATIO = 1.15


def music_target_seconds(script: str = "") -> int:
    """Durée cible de la musique, déduite du nombre de mots des VO du script.

    Repli à 30s si le script est vide/illisible. Borné à [15s, 90s] pour ne
    jamais demander un rendu déraisonnable à ACE-Step."""
    from nodes.scriptwriter.script_timing import parse_plans, _count_words
    words = 0
    try:
        for p in parse_plans(script or ""):
            for vo in p["vo_lines"]:
                words += _count_words(vo["text"])
    except Exception:
        words = 0
    if words <= 0:
        return 30
    target = int(round(words / WORDS_PER_SEC * MUSIC_MARGIN_RATIO))
    return max(MUSIC_MIN_SEC, min(MUSIC_MAX_SEC, target))

ACE_STEP_FILES = {
    "unet": Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/models/diffusion_models/acestep_v1.5_xl_turbo_bf16.safetensors"),
    "vae": Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/models/vae/ace_1.5_vae.safetensors"),
    "te1": Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/models/text_encoders/qwen_0.6b_ace15.safetensors"),
    "te2": Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/models/text_encoders/qwen_4b_ace15.safetensors"),
}

MUSIC_ENGINE = os.getenv("PF_MUSIC_ENGINE", "acestep_cpp")  # "acestep_cpp" | "comfyui"

# Profils musicaux validés pour reels/TikTok (acestep.cpp). Le prompt est verrouillé
# (métadonnées bpm/keyscale/timesignature/duration jamais réécrites par le LM).
MUSIC_PROFILES = {
    "phonk": {
        "prompt": ("Aggressive phonk track. Heavy distorted 808 bass, cowbell melody, "
                   "dark detuned synth, fast hi-hat rolls, gritty Memphis rap drums, "
                   "lo-fi vinyl texture, menacing driving energy, underground trap beat."),
        "bpm": 140, "keyscale": "B minor",
    },
    "deephouse": {
        "prompt": ("Deep house track. Warm analog synth chords, groovy bassline, "
                   "smooth four-on-the-floor kick, filtered pads, jazzy keys, "
                   "soulful summer vibe, hypnotic minimal groove, DJ set club sound."),
        "bpm": 122, "keyscale": "D minor",
    },
    "melodictrap": {
        "prompt": ("Melodic trap beat. Warm 808 bass, smooth piano chords, airy synth "
                   "melody, soft hi-hats, gentle snare, emotional hip-hop production, "
                   "radio-friendly modern rap instrumental, clean mix, heartfelt vibe."),
        "bpm": 132, "keyscale": "D minor",
    },
    "synthwave": {
        "prompt": ("Synthwave retrowave track. Nostalgic analog synth arpeggios, driving "
                   "electric bass line, steady drum machine beat, neon 80s vibe, dreamy "
                   "pads, palm-muted guitar accents, cinematic retro electronic soundtrack."),
        "bpm": 100, "keyscale": "A minor",
    },
    "rnb": {
        "prompt": ("Modern R&B pop instrumental. Smooth electric piano, mellow 808 bass, "
                   "laid-back trap-soul drums, warm vocal chops, airy pads, sultry evening "
                   "groove, contemporary mainstream production, clean professional mix."),
        "bpm": 95, "keyscale": "F minor",
    },
    "anime": {
        "prompt": ("Upbeat anime vlog background music, fast 128 bpm driving rhythm, "
                   "Future Funk and modern Electro-Pop, subtle sidechained synth bass, "
                   "rhythmic hi-hats, catchy light synth pads, playful and energetic vibe, "
                   "clean arrangement, no loud solos, no brass, continuous steady momentum, "
                   "video game news background track, perfect ducking, high energy."),
        "bpm": 128, "keyscale": "A minor",
    },
    "videogame": {
        "prompt": ("Retro arcade chiptune track with modern energy. Punchy square-wave lead "
                   "melody, fast breakbeat drums, driving synth bass, 16-bit video game "
                   "console soundtrack, upbeat playful catchy main theme, quick tempo, "
                   "energetic and bright."),
        "bpm": 140, "keyscale": "C major",
    },
}

# Tags Audio: du chemin alt (script_generator_alt) -> profil péchu/anime.
# Déterministe et prioritaire sur le mood (le LLM n'invente pas le genre).
# Clefs normalisées : minuscules, sans espaces.
AUDIO_TAG_PROFILES = [
    ("upbeat_gaming", "anime"),
    ("upbeat", "anime"),
    ("anime_opening", "anime"),
    ("anime_battle", "anime"),
    ("jrpg_battle", "anime"),
    ("jrpg", "anime"),
    ("gaming", "anime"),
    ("peppy", "anime"),
    ("heroic", "anime"),
    ("videogame", "videogame"),
    ("video_game", "videogame"),
    ("chiptune", "videogame"),
    ("arcade", "videogame"),
]

# Mapping mots-clés (mood du slot audio) -> profil. Ordre = priorité.
MOOD_KEYWORDS = [
    ("anime", ["anime", "jrpg", "jeu vidéo", "jeu video", "video game", "gaming", "upbeat", "heroic", "shonen", "peppy", "péchu", "punchy"]),
    ("phonk", ["phonk", "agressif", "dark", "trap", "menac", "energie", "sport", "vitesse", "adrenal"]),
    ("melodictrap", ["emo", "sentiment", "rap", "melancol", "nostal", "piano", "heartfelt", "emotion"]),
    ("synthwave", ["retro", "80s", "neon", "synthwave", "rétro", "néon", "vintage", "futurist"]),
    ("rnb", ["rnb", "r&b", "smooth", "sexy", "chill", "sultry", "doux", "night", "soir"]),
    ("deephouse", ["deep", "house", "groove", "club", "dance", "chaud", "été", "summer"]),
]


def _pick_profile(mood: str, tag: str = "") -> dict:
    """Choisit un profil musical selon le tag `Audio:` du script, puis le mood (texte libre).

    Le tag du chemin alt (ex. UPBEAT_GAMING) fait foi : profil anime/upbeat garanti,
    avant toute interprétation LLM du mood. Les vibes classiques (phonk, deephouse,
    melodic…) sont préservées en l'absence de tag upbeat."""
    normalized_tag = (tag or "").strip().lower().replace(" ", "")
    for tag_key, profile in AUDIO_TAG_PROFILES:
        if tag_key in normalized_tag:
            p = dict(MUSIC_PROFILES[profile])
            log.info(f"MusicGenerator: tag '{tag}' -> profile '{profile}'")
            return p

    m = (mood or "").lower()
    for profile, keywords in MOOD_KEYWORDS:
        if any(k in m for k in keywords):
            p = dict(MUSIC_PROFILES[profile])
            log.info(f"MusicGenerator: mood '{mood}' -> profile '{profile}'")
            return p
    default = dict(MUSIC_PROFILES["melodictrap"])
    log.info(f"MusicGenerator: mood '{mood}' -> default 'melodictrap'")
    return default


def _ace_step_available() -> bool:
    """True si les 4 fichiers ACE-Step 1.5 (UNET + VAE + 2 text encoders) sont présents."""
    missing = [k for k, p in ACE_STEP_FILES.items() if not p.is_file()]
    if missing:
        log.warning(f"MusicGenerator: ACE-Step 1.5 incomplet, manquant: {missing}")
        return False
    return True


class MusicGenerator(AsyncNode):
    """Génère les pistes musicales via ACE-Step 1.5.

    Moteur par défaut : acestep.cpp (GGUF/Vulkan, ~20 s / piste). Fallback
    configurable via PF_MUSIC_ENGINE=comfyui (workflow generate_song_15, lent).

    Lit les slots `audio` (mood) du blueprint d'AssetPlanner, choisit un profil
    musical adapté, génère chaque piste et alimente shared["downloaded_audio"]."""

    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "music_generator"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        audio_slots = blueprint.get("audio", [])
        if not audio_slots:
            log.warning("MusicGenerator: no audio slots in blueprint")
            return json.dumps({"downloaded_audio": []}, ensure_ascii=False)

        slots_to_regenerate = shared.get("_slots_to_regenerate", [])
        if slots_to_regenerate:
            audio_slots = [s for s in audio_slots if s.get("id") in slots_to_regenerate]
            log.info(f"MusicGenerator: partial regen for slots {slots_to_regenerate}")

        engine = MUSIC_ENGINE
        if engine == "acestep_cpp" and not _ace_cpp_available():
            log.warning(f"MusicGenerator: acestep.cpp non disponible, fallback ComfyUI")
            engine = "comfyui"
        if engine == "comfyui" and not _ace_step_available():
            log.warning("MusicGenerator: ComfyUI ACE-Step 1.5 absent, skipping music generation")
            return json.dumps({"downloaded_audio": []}, ensure_ascii=False)

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        music_sec = music_target_seconds(shared.get("script", ""))
        log.info(f"MusicGenerator: durée cible {music_sec}s (déduite des mots des VO)")

        generated = []
        feedback = shared.get("af_feedback", "")
        for slot in audio_slots:
            slot_id = slot.get("id", "unknown")
            mood = slot.get("mood", slot.get("content", "background music"))
            profile = _pick_profile(mood, slot.get("content", ""))
            if feedback:
                profile = {**profile, "prompt": f"{profile['prompt']}. Avoid: {feedback}"}
            lyrics = slot.get("content", "Instrumental track.")

            log.info(f"Generating music (engine={engine}) for slot {slot_id}: {mood[:60]}...")
            if engine == "acestep_cpp":
                path = await acestep_generate_music(
                    prompt=profile["prompt"],
                    lyrics="",
                    dest_dir=dest,
                    name=f"music_{slot_id}",
                    bpm=profile["bpm"],
                    keyscale=profile["keyscale"],
                    timesignature=4,
                    duration=music_sec,
                )
            else:
                path = await comfyui_generate_song_15(
                    tags=profile["prompt"],
                    lyrics=lyrics,
                    dest_dir=dest,
                    name=f"music_{slot_id}",
                    seconds=float(music_sec),
                )
            generated.append({
                "slot_id": slot_id,
                "type": "music",
                "section": slot.get("section", ""),
                "position": slot.get("position", 0),
                "mood": mood,
                "query": mood,
                "path": path,
            })

        ok = [g for g in generated if g.get("path")]
        log.info(f"MusicGenerator -> {len(ok)}/{len(generated)} music tracks generated")
        return json.dumps({"downloaded_audio": generated}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("MusicGenerator POST -> exec is not valid JSON")
            shared["_current_step"] = "music_generator_error"
            shared["_error"] = "MusicGenerator: exec is not valid JSON"
            shared["steps"].append({
                "step": "music_generator", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("MusicGenerator aborted: exec is not valid JSON")

        new_audio = data.get("downloaded_audio", [])
        partial = shared.get("_slots_to_regenerate")
        if partial:
            prev = shared.get("downloaded_audio", [])
            new_ids = {a.get("slot_id") for a in new_audio}
            kept = [a for a in prev if a.get("slot_id") not in new_ids]
            shared["downloaded_audio"] = kept + new_audio
            log.info(f"MusicGenerator: merged {len(new_audio)} regenerated + {len(kept)} kept")
        else:
            prev = shared.get("downloaded_audio", [])
            shared["downloaded_audio"] = prev + new_audio
        shared["_current_step"] = "music_generator_done"
        shared["steps"].append({
            "step": "music_generator", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(shared['downloaded_audio'])} audio tracks (music)",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        if shared.get("_slots_to_regenerate"):
            return "regen_done"
        return "default"