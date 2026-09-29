import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.comfyui_api import (
    comfyui_generate_voice,
    s2_generate_voice,
    S2_BIN,
    S2_GGUF,
    S2_TOKENIZER,
    S2_VOICE_PROFILES,
)

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"

VOICE_BACKEND = "s2"  # "s2" = Fish Speech S2 (s2.cpp/GGAU) ; "fish15" = Fish Speech 1.5 (ComfyUI)
S2_VOICE = os.getenv("PF_S2_VOICE", "woman_news")  # profil .s2voice cloné (voir s2.cpp/voice_profiles)

FISH_FILES = {
    "model": Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/custom_nodes/ComfyUI_FishSpeech_EX/checkpoints/fish-speech-1.5/model.pth"),
    "vqgan": Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/custom_nodes/ComfyUI_FishSpeech_EX/checkpoints/firefly-gan-vq-fsq-8x1024-21hz-generator.pth"),
    "ref_voice": Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/input/ref_voice/ref_voice.wav"),
}


def _fish_available() -> bool:
    """True si Fish Speech 1.5 (LLaMA + VQGAN) et la voix de référence sont présents."""
    missing = [k for k, p in FISH_FILES.items() if not p.is_file()]
    if missing:
        log.warning(f"VoiceGenerator: Fish Speech 1.5 incomplet, manquant: {missing}")
        return False
    return True


def _s2_available() -> bool:
    """True si Fish Speech S2 (binaire s2.cpp + GGUF + tokenizer) et le profil de voix cloné sont présents."""
    profile = S2_VOICE_PROFILES / f"{S2_VOICE}.s2voice"
    if not profile.is_file():
        log.warning(f"VoiceGenerator: profil s2 introuvable: {profile}")
        return False
    missing = [str(p) for p in (S2_BIN, S2_GGUF, S2_TOKENIZER) if not p.is_file()]
    if missing:
        log.warning(f"VoiceGenerator: Fish Speech S2 incomplet, manquant: {missing}")
        return False
    return True


class VoiceGenerator(AsyncNode):
    """Génère les voix off via Fish Speech 1.5 + ComfyUI (workflow generate_voice).

    Lit les slots `voiceover` du blueprint d'AssetPlanner, génère chaque piste avec
    comfyui_generate_voice, et alimente shared["downloaded_audio"] (type="voiceover",
    une piste par section Hook/Body/CTA). Skip proprement si Fish n'est pas installé."""

    def __init__(self):
        super().__init__(max_retries=1, wait=10)

    async def prep_async(self, shared):
        shared["_current_step"] = "voice_generator"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        vo_slots = blueprint.get("voiceover", [])
        if not vo_slots:
            log.warning("VoiceGenerator: no voiceover slots in blueprint")
            return json.dumps({"downloaded_audio": []}, ensure_ascii=False)

        slots_to_regenerate = shared.get("_slots_to_regenerate", [])
        if slots_to_regenerate:
            vo_slots = [s for s in vo_slots if s.get("id") in slots_to_regenerate]
            log.info(f"VoiceGenerator: partial regen for slots {slots_to_regenerate}")

        if VOICE_BACKEND == "s2":
            if not _s2_available():
                log.warning("VoiceGenerator: Fish Speech S2 absent, skipping voiceover")
                return json.dumps({"downloaded_audio": []}, ensure_ascii=False)
        elif not _fish_available():
            log.warning("VoiceGenerator: Fish Speech 1.5 absent, skipping voiceover")
            return json.dumps({"downloaded_audio": []}, ensure_ascii=False)

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest = DOWNLOADS_DIR / pipeline_id
        dest.mkdir(parents=True, exist_ok=True)

        generated = []
        feedback = shared.get("af_feedback", "")
        for slot in vo_slots:
            slot_id = slot.get("id", "unknown")
            text = slot.get("text", "")
            if not text.strip():
                log.warning(f"Voiceover slot {slot_id}: empty text, skipping")
                continue
            if feedback:
                log.info(f"Voiceover [{slot_id}] feedback: {feedback[:100]}")

            log.info(f"Generating voiceover for slot {slot_id} ({slot.get('section', '')}): {text[:80]}...")
            temperature = float(slot.get("temperature", 0.0) or 0.0)  # 0 = défaut config (S2_TEMPERATURE)
            if VOICE_BACKEND == "s2":
                path = await s2_generate_voice(
                    text=text,
                    dest_dir=dest,
                    name=f"vo_{slot_id}",
                    voice=S2_VOICE,
                    voice_dir=S2_VOICE_PROFILES,
                    temperature=temperature,
                )
            else:
                path = await comfyui_generate_voice(
                    text=text,
                    dest_dir=dest,
                    name=f"vo_{slot_id}",
                    temperature=temperature,
                )
            generated.append({
                "slot_id": slot_id,
                "type": "voiceover",
                "section": slot.get("section", ""),
                "position": slot.get("position", 0),
                "text": text,
                "path": path,
            })

        ok = [g for g in generated if g.get("path")]
        log.info(f"VoiceGenerator -> {len(ok)}/{len(generated)} voiceover tracks generated")
        return json.dumps({"downloaded_audio": generated}, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("VoiceGenerator POST -> exec is not valid JSON")
            shared["_current_step"] = "voice_generator_error"
            shared["_error"] = "VoiceGenerator: exec is not valid JSON"
            shared["steps"].append({
                "step": "voice_generator", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VoiceGenerator aborted: exec is not valid JSON")

        new_audio = data.get("downloaded_audio", [])
        partial = shared.get("_slots_to_regenerate")
        if partial:
            prev = shared.get("downloaded_audio", [])
            new_ids = {a.get("slot_id") for a in new_audio}
            kept = [a for a in prev if a.get("slot_id") not in new_ids]
            shared["downloaded_audio"] = kept + new_audio
            log.info(f"VoiceGenerator: merged {len(new_audio)} regenerated + {len(kept)} kept")
        else:
            shared["downloaded_audio"] = shared.get("downloaded_audio", []) + new_audio
        shared["_current_step"] = "voice_generator_done"
        shared["steps"].append({
            "step": "voice_generator", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(shared['downloaded_audio'])} audio tracks (music + voiceover)",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        if shared.get("_slots_to_regenerate"):
            return "regen_done"
        return "default"
