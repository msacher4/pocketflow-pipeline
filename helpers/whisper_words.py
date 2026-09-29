"""Transcription de mots avec timestamps via faster-whisper (sous-process).

Le pipeline tourne en python système (sans torch). On exécute faster-whisper
dans le venv dédié `/media/marcs/Linux_Apps/Projets_AI/Faster-WHisper/`
et on récupère le résultat en JSON.
"""
import asyncio
import json
import logging
import os
import sys

log = logging.getLogger("pocketflow-pipeline")

# Cache mémoire : chemin fichier → [{word, start_s, end_s}]
_cache: dict[str, list[dict]] = {}


def _merge_subwords(raw: list[dict]) -> list[dict]:
    """Fusionne les tokens sub-word de whisper en mots complets.

    Whisper split les contractions françaises : "j'aurais" → "j", "'aurais".
    On fusionne uniquement si le token commence par une apostrophe/tiret
    (signe clair d'un sous-mot)."""
    if not raw:
        return []
    merged = []
    buf = raw[0].copy()
    for tok in raw[1:]:
        word = tok["word"]
        prev = buf["word"]
        # Fusionner si le token commence par apostrophe ou tiret
        if word.startswith("'") or word.startswith("-"):
            buf["word"] = prev + word
            buf["end_s"] = tok["end_s"]
        else:
            merged.append(buf)
            buf = tok.copy()
    merged.append(buf)
    return merged


async def transcribe_words(
    audio_path: str,
    whisper_bin: str | None = None,
    model_dir: str | None = None,
    model_name: str | None = None,
    language: str | None = None,
) -> list[dict] | None:
    """Transcrit un fichier audio et retourne les mots avec timestamps.

    Args:
        audio_path: chemin du fichier audio (mp3/wav/etc.)
        whisper_bin: chemin du python du venv faster-whisper
        model_dir: dossier contenant le modèle (ex: /media/.../FAster-WHisper)
        model_name: nom du modèle (défaut: "small")
        language: langue forcée (None = auto-détection)

    Returns:
        list de {word, start_s, end_s} ou None si échec
    """
    if audio_path in _cache:
        return _cache[audio_path]

    if not os.path.isfile(audio_path):
        log.warning(f"whisper_words: file not found: {audio_path}")
        return None

    from config import WHISPER_VENV, WHISPER_MODEL_DIR, WHISPER_MODEL_NAME, WHISPER_LANGUAGE

    bin_python = whisper_bin or WHISPER_VENV
    mdir = model_dir or WHISPER_MODEL_DIR
    mname = model_name or WHISPER_MODEL_NAME
    lang = language if language is not None else WHISPER_LANGUAGE

    model_path = os.path.join(mdir, mname)
    if not os.path.isfile(bin_python):
        log.warning(f"whisper_words: python not found: {bin_python}")
        return None
    if not os.path.isdir(model_path):
        log.warning(f"whisper_words: model not found: {model_path}")
        return None

    # Script Python exécuté dans le venv
    script = f'''
import json, sys
from faster_whisper import WhisperModel

model = WhisperModel("{model_path}", device="cpu", compute_type="int8")
lang = {repr(lang)} if {repr(lang)} is not None else None
segments, info = model.transcribe("{audio_path}", language=lang, word_timestamps=True)

words = []
for seg in segments:
    if seg.words:
        for w in seg.words:
            words.append({{"word": w.word, "start_s": round(w.start, 3), "end_s": round(w.end, 3)}})

print(json.dumps(words, ensure_ascii=False))
'''
    proc = await asyncio.create_subprocess_exec(
        bin_python, "-c", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        log.error(f"whisper_words: timeout for {audio_path}")
        proc.kill()
        return None

    if proc.returncode != 0:
        log.error(f"whisper_words: failed ({proc.returncode}): {stderr.decode(errors='replace')[:500]}")
        return None

    try:
        raw_words = json.loads(stdout.decode().strip())
        words = _merge_subwords(raw_words)
        _cache[audio_path] = words
        log.info(f"whisper_words: {audio_path} -> {len(words)} words (merged from {len(raw_words)} tokens)")
        return words
    except json.JSONDecodeError as e:
        log.error(f"whisper_words: invalid JSON from whisper: {e}")
        return None
