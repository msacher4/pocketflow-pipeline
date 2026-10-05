import json
import logging
import re
from datetime import datetime, timezone

import asyncio

from pydantic import BaseModel, field_validator
from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot
from nodes.scriptwriter.script_timing import static_video_reason

log = logging.getLogger("pocketflow-pipeline")

_SECTION_RE = re.compile(r"^#{1,6}\s*\(?([^\)(]*)\s*\)?", re.IGNORECASE)
_VIDEO_RE = re.compile(r"^\s*[-*]?\s*Video\s*:\s*.+$", re.IGNORECASE)
_VO_RE = re.compile(r"^\s*[-*]?\s*VO\s*:\s*.+$", re.IGNORECASE)
_SFX_RE = re.compile(r"^\s*[-*]?\s*SFX\s*:\s*.+$", re.IGNORECASE)
_AUDIO_RE = re.compile(r"^\s*[-*]?\s*Audio\s*:\s*.+$", re.IGNORECASE)

_REQUIRED_SECTIONS = {"hook", "body", "cta"}


class GeneratedScript(BaseModel):
    script: str

    @field_validator("script")
    @classmethod
    def script_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("script must not be empty")
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_has_asset_structure(cls, v):
        if not v or not v.strip():
            return v

        sections_found = set()
        has_video = False
        has_vo = False
        has_audio = False

        for line in v.splitlines():
            raw = line
            m = _SECTION_RE.match(raw)
            if m and m.group(1).strip():
                key = m.group(1).strip().lower().split("(")[0].strip()
                for req in _REQUIRED_SECTIONS:
                    if req in key:
                        sections_found.add(req)
            if not has_video and _VIDEO_RE.match(raw):
                has_video = True
            if not has_vo and _VO_RE.match(raw):
                has_vo = True
            if not has_audio and _AUDIO_RE.match(raw):
                has_audio = True

        missing = []
        if not has_video:
            missing.append("aucune ligne Video:")
        if not has_vo:
            missing.append("aucune ligne VO:")
        if not sections_found:
            missing.append("aucune section (### HOOK / ### BODY / ### CTA)")
        if not has_audio:
            missing.append("aucune ligne Audio:")

        critical = [m for m in missing if "Video" in m or "VO" in m]
        if critical:
            raise ValueError(
                f"Script sans structure d'assets détectée — "
                f"{' ; '.join(critical)}. "
                f"Format requis : lignes Video:/VO: + sections ### HOOK/BODY/CTA"
            )
        return v.strip()


# Vocabulaire du texte interdit dans les descriptions d'assets vidéo (Video:).
# Le modèle vidéo gère très mal le texte : tout texte, titre, mot affiché est banni.
_VIDEO_TEXT_TOKENS = (
    "text", "title", "headline", "caption", "subtitle", "subtitles",
    "label", "labels", "word", "words", "sentence", "heading",
    "typography", "font", "logo text", "text overlay", "overlay",
    "titre", "phrase", "mots", "inscription", "slogan", "timestamp",
    "date", "nombre", "chiffre",
)


class GeneratedScriptAlt(GeneratedScript):
    """Version alt : valide la structure + interdit tout texte/titre dans les assets vidéo."""

    @field_validator("script")
    @classmethod
    def script_at_least_7_plans(cls, v):
        """Une vidéo développée demande au moins 7 plans (1 hook + ≥4 body + 1 cta)."""
        if not v or not v.strip():
            return v
        import re as _re
        plan_lines = [
            line for line in v.splitlines()
            if _re.match(r"^\s*[-*]?\s*Plan\s*\d+\s*\(", line)
        ]
        if len(plan_lines) < 7:
            raise ValueError(
                f"Vidéo trop courte : {len(plan_lines)} plan(s) détecté(s), minimum 7 plans "
                "exigé (1 HOOK + au moins 4 BODY + 1 CTA). Développe l'info en plus de plans, "
                "chacun avec son titre 'Plan N (T-Ts)' et ses lignes Video:/VO:."
            )
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_no_text_overlay(cls, v):
        if not v or not v.strip():
            return v
        import re as _re
        for line in v.splitlines():
            if not _re.match(r"^\s*[-*]?\s*Video\s*:", line, _re.IGNORECASE):
                continue
            low = line.lower()
            # Matching sur MOT (boundaries \b), pas substring : "word" ne doit pas
            # matcher "sword", "copy" ne doit pas matcher "copyright", etc.
            for tok in _VIDEO_TEXT_TOKENS:
                pat = r"\b" + _re.escape(tok) + r"\b"
                if _re.search(pat, low):
                    raise ValueError(
                        f"Texte interdit dans Video: : la ligne contient '{tok}'. Les assets "
                        "vidéo doivent être 100 % visuels (décors, personnages, actions), "
                        "sans aucun texte/titre affiché à l'écran."
                    )
            if '"' in line or "“" in line or "\u201c" in line:
                raise ValueError(
                    "Texte interdit dans Video: : la ligne contient du texte entre guillemets. "
                    "Les assets vidéo doivent être 100 % visuels, sans aucun texte à l'écran."
                )
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_video_ltx_ready(cls, v):
        """Filet LTX-2.5 officiel (docs.ltx.io) : les lignes Video: doivent être
        des paragraphes contigus au présent, SANS brackets/timcodes `[0-1s]`
        (labels nuisibles que LTX ignore ou qui effondrent la génération), sans
        ouverture filler ('A video of…' / 'cinematic shot of…'), assez denses pour
        porter action + décor + caméra (>= 3 phrases ou >= 25 mots).

        Erreurs préfixées "ASSET(plan N)" pour que le routing les envoie vers le
        ScriptFixer (réécriture ciblée de la SEULE ligne fautive) au lieu de
        régénérer tout le script via AltSG."""
        if not v or not v.strip():
            return v
        import re as _re
        current_plan = None
        for line in v.splitlines():
            m = _re.match(r"^\s*[-*]?\s*Plan\s*(\d+)\s*\(", line)
            if m:
                current_plan = int(m.group(1))
                continue
            if not _re.match(r"^\s*[-*]?\s*Video\s*:", line, _re.IGNORECASE):
                continue
            low = line.lower()
            if _re.search(r"\[\s*\d+\s*[-–]\s*\d+\s*s?\]", line) or "[" in line:
                raise ValueError(
                    f"ASSET(plan {current_plan}): brackets/timcode `[0-1s]` interdit dans "
                    "Video: — ce sont des LABELS nuisibles pour LTX-2.5 (génération ignorée "
                    "ou effondrée). Écris la chronologie en toutes lettres (then, a moment "
                    "later), en UN paragraphe continu au présent, 4-8 phrases, aucun crochet."
                )
            if low.startswith("a video of") or low.startswith("cinematic shot of"):
                raise ValueError(
                    f"ASSET(plan {current_plan}): ouverture interdite dans Video: — jamais "
                    "'A video of…' ni 'cinematic shot of…' (filler de modèle). Ouvre par "
                    "l'action."
                )
            n_ends = len(_re.findall(r"[.!?]+", line))
            n_words = len(line.split())
            if n_ends < 3 and n_words < 25:
                raise ValueError(
                    f"ASSET(plan {current_plan}): Video: trop courte ({n_words} mots, "
                    f"{n_ends} phrase(s)) — LTX-2.5 veut UN paragraphe fluide de 4-8 phrases "
                    "au présent (action, décor physique, perso, caméra, son, lumière). "
                    "Développe la ligne."
                )
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_video_no_static(cls, v):
        """Une ligne Video: figée est interdite par la règle 12 (dynamisme
        obligatoire) — LTX ne rend rien d'intéressant.

        Le contrôle est délégué à `script_timing.static_video_reason`, source
        UNIQUE partagée avec le ScriptFixer. L'ancienne regex locale rejetait
        `frozen` sur TOUTE la ligne, donc aussi un décor (« a vast frozen
        throne hall ») dont le personnage était en mouvement : 6 régénérations
        AltSG,~39 min, erreur identique à chaque cycle (run 20261004_131919)."""
        if not v or not v.strip():
            return v
        current_plan = None
        for line in v.splitlines():
            m = re.match(r"^\s*[-*]?\s*Plan\s*(\d+)\s*\(", line)
            if m:
                current_plan = int(m.group(1))
                continue
            if not re.match(r"^\s*[-*]?\s*Video\s*:", line, re.IGNORECASE):
                continue
            reason = static_video_reason(line)
            if reason:
                # PRÉFIXE `ASSET(plan N):` OBLIGATOIRE — `is_asset_error()` teste
                # "asset(plan" pour router vers le ScriptFixer ; sans lui l'erreur
                # retombe dans le `else` → régénération AltSG complète.
                raise ValueError(
                    f"ASSET(plan {current_plan}): plan FIGÉ interdit dans Video: "
                    f"{reason}. Réécris la ligne avec de l'énergie explicite."
                )
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_single_audio(cls, v):
        """Une seule musique (Audio:) pour toute la vidéo, déclarée au début du script."""
        if not v or not v.strip():
            return v
        import re as _re
        audio_positions = []
        for idx, line in enumerate(v.splitlines()):
            if _re.match(r"^\s*[-*]?\s*Audio\s*:", line, _re.IGNORECASE):
                audio_positions.append(idx)
        if not audio_positions:
            raise ValueError(
                "Aucune musique déclarée : le script doit contenir UNE ligne Audio: "
                "(une seule musique couvrant toute la vidéo), en tête de script."
            )
        if len(audio_positions) > 1:
            raise ValueError(
                "Plusieurs musiques déclarées : il doit y avoir UNE SEULE ligne Audio: "
                "pour toute la vidéo (pas de changement de musique par section)."
            )
        # La musique doit être au tout début (avant toute section ### ).
        lines = v.splitlines()
        first_section = next(
            (i for i, l in enumerate(lines)
             if _re.match(r"^\s*#{1,6}\s*", l)), None)
        if first_section is not None and audio_positions[0] > first_section:
            raise ValueError(
                "La ligne Audio: doit être placée au tout début du script, avant la "
                "première section (### HOOK/BODY/CTA)."
            )
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_max_two_video_per_plan(cls, v):
        """Max 2 vidéos (lignes Video:) par plan — un beat de 8s (2 assets)
        suffit pour une VO courte. Jamais de 3e, 4e asset pour allonger."""
        if not v or not v.strip():
            return v
        import re as _re
        plan_videos: dict[int, int] = {}
        cur = None
        for line in v.splitlines():
            m = _re.match(r"^\s*[-*]?\s*Plan\s*(\d+)\s*\(", line)
            if m:
                cur = int(m.group(1))
                plan_videos.setdefault(cur, 0)
                continue
            if cur is not None and _re.match(r"^\s*[-*]?\s*Video\s*:", line, _re.IGNORECASE):
                plan_videos[cur] = plan_videos.get(cur, 0) + 1
        for num, n in plan_videos.items():
            if n > 2:
                raise ValueError(
                    f"Plan {num} : {n} lignes Video: détectées — maximum 2 vidéos par "
                    "plan (≤ ≈18-20 mots de VO à 2 mots/sec). Garde au plus 2 assets: "
                    "retire les vidéos excédentaires et raccourcis la VO si besoin."
                )
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_no_empty_filler(cls, v):
        """Interdit les VO sensationnalistes vides qui ne nomment ni fait, ni
        enjeu, ni entité concrète — pour éviter les 30s qui ne disent rien."""
        if not v or not v.strip():
            return v
        import re as _re
        _FILLER_TOKENS = (
            "rises from the ashes", "from the ashes", "breaks the rules",
            "finally gets its chance", "gets its chance", "finally makes history",
            "makes history", "the legend awakens", "awakens",
            "rises to power", "reclaims her", "her time has come",
            "a legend is born", "power awakens", "destiny calls",
            "dark power", "a new era", "dawn of",
        )
        for line in v.splitlines():
            if not _re.match(r"^\s*[-*]?\s*VO\s*:", line, _re.IGNORECASE):
                continue
            low = line.lower()
            for tok in _FILLER_TOKENS:
                if tok in low:
                    raise ValueError(
                        f"VO sensationnaliste vide interdite : '{tok}'. Chaque VO doit "
                        "apporter une information concrète (nom du personnage, franchise, "
                        "fait, chiffre, enjeu) tirée de l'article. Pas de poésie/filler "
                        "générique qui dirait n'importe quoi."
                    )
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_vo_short(cls, v):
        """Filet de sécurité absolu : une VO au-delà de 20 mots ne rentre même
        pas sur 2 vidéos par plan (2 assets = 8s ≈ 20 mots, 1 asset = 4s ≈ 12 mots)
        à 2 mots/sec + filet +4. Doit être simplifiée — on ne splitte jamais."""
        if not v or not v.strip():
            return v
        import re as _re
        for line in v.splitlines():
            if not _re.match(r"^\s*[-*]?\s*VO\s*:", line, _re.IGNORECASE):
                continue
            text = _re.sub(r"^\s*[-*]?\s*VO\s*:\s*", "", line, flags=_re.IGNORECASE)
            n_words = len([w for w in text.replace("'", " ").split() if w.strip()])
            if n_words > 20:
                raise ValueError(
                    f"VO beaucoup trop longue ({n_words} mots — 2 vidéos par plan "
                    "ne couvrent que ≈ 18-20 mots à 2 mots/sec, jamais de split): "
                    f"'{text[:80]}'. Simplifie la phrase pour qu'elle tienne sur "
                    "un maximum de 2 vidéos par plan, sans jamais être coupée."
                )
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_vo_plan_duration(cls, v):
        """Chaque VO doit tenir dans la durée de SON plan. La durée d'un plan = la
        SOMME des durées de ses assets (I2V≈4s, T2V≈4s). Si la phrase dépasse, le
        beat doit recevoir des assets vidéo supplémentaires — jamais tronqué."""
        from nodes.scriptwriter.script_timing import pacing_errors
        errs = pacing_errors(v)
        if errs:
            raise ValueError(errs[0])
        return v.strip()

    @field_validator("script")
    @classmethod
    def script_vo_no_redundancy(cls, v):
        """Rejette les VO qui répètent le même concept (chaque VO doit justifier
        son existence : un fait/une émotion/un enjeu UNIQUE par plan)."""
        if not v or not v.strip():
            return v
        import re as _re
        lines = v.splitlines()
        vo_lines = [
            _re.sub(r"^\s*[-*]?\s*VO\s*:\s*", "", l, flags=_re.IGNORECASE)
            for l in lines
            if _re.match(r"^\s*[-*]?\s*VO\s*:", l, _re.IGNORECASE)
        ]
        if len(vo_lines) < 2:
            return v

        # Groupes de concepts : si le même groupe apparaît dans >= 3 VO distinctes,
        # ou dans 2 VO CONSECUTIVES, c'est un doublon narratif.
        _VO_CONCEPT_GROUPS = (
            ("première fois jouable", ("never playable", "never been playable",
                                       "first playable", "finally playable", "playable",
                                       "not playable")),
            ("version corrompue", ("corrupted", "corrupt", "dark ver",
                                   "dark side", "fallen")),
            ("fandom divisé", ("fandom", "fans", "community", "players",
                               "split", "divided", "divide", "two camps",
                               "two sides", "camps", "sides")),
            ("choix identitaire", ("choose", "choice", "pick", "choosing",
                                   "your side", "pick your")),
            ("villain devenu héroïne", ("villain", "antagonist", "enemy",
                                        "heroine", "protagonist")),
            ("héroïne iconique", ("iconic", "legend", "mythic", "classic",
                                  "most loved", "beloved")),
            ("attente / retour", ("wait", "waiting", "awaited", "years of",
                                  "long awaited", "finally")),
            ("déshonorée/interdite", ("forbidden", "interdite", "taboo",
                                      "banned", "censored")),
            ("power/destin", ("power", "destiny", "fate", "destined",
                              "awakening")),
        )
        # La dernière VO (CTA) est le bouclage/l'engagement : il est NORMAL qu'elle
        # re-pique le concept porté par le body (strategy=loop). Elle est exempte
        # du contrôle de redondance (consecutif ET seuil >=3).
        cta_idx = len(vo_lines) - 1
        for grp_name, toks in _VO_CONCEPT_GROUPS:
            hits = [i for i, vo in enumerate(vo_lines) if any(t in vo for t in toks)]
            body_hits = [i for i in hits if i < cta_idx]
            if len(body_hits) >= 3:
                raise ValueError(
                    f"VO redondantes : le concept '{grp_name}' apparaît dans {len(body_hits)} "
                    "VO distinctes. Chaque VO doit apporter UN fait/une émotion/une boucle "
                    "UNIQUE. Garde la meilleure occurrence et réécris les autres avec un "
                    "info/angle différent."
                )
            for a, b in zip(hits, hits[1:]):
                if b == a + 1 and b < cta_idx:
                    raise ValueError(
                        f"VO qui se répètent : concept '{grp_name}' dans 2 plans consécutifs. "
                        "Une VO qui reformule la précédente est un doublon interdit — "
                        "donne à chaque plan une info/émotion différente."
                    )

        # Répétition d'un mot-clé fort (>= 4 lettres, hors stopwords/noms systèmes) dans
        # >= 3 VO distinctes → stagne.
        _STOPWORDS = {
            "the", "and", "for", "with", "that", "this", "from", "her", "his",
            "she", "was", "not", "are", "you", "your", "its", "now", "get", "has",
            "had", "her", "they", "them", "then", "than", "but", "were", "been",
            "will", "would", "can", "could", "into", "onto", "over", "under",
        }
        _SAFE_NAMES = {"saber", "alter", "fate", "extra", "record", "waifu",
                       "tiktok", "game", "franchise", "character", "characters"}
        word_hits: dict[str, list[int]] = {}
        for i, vo in enumerate(vo_lines):
            seen = set()
            for w in _re.findall(r"[a-z]{4,}", vo.lower()):
                if w in _STOPWORDS or w in _SAFE_NAMES or w in seen:
                    continue
                seen.add(w)
                word_hits.setdefault(w, []).append(i)
        for w, idxs in word_hits.items():
            if len(idxs) >= 3:
                raise ValueError(
                    f"Mot-clé répété '{w}' dans {len(idxs)} VO distinctes. Une vidéo qui "
                    "stagne sur le même mot ne baille pas. Réécris pour que chaque VO "
                    "apporte une info et un lexique différents."
                )
        return v.strip()


def is_vo_length_error(err: str) -> bool:
    """True si l'erreur pydantic est une VO trop longue — le seul cas que le
    VoShortener sait réparer (raccourcissement ciblé des VO, structure intacte).

    Tout le reste (nombre de plans, overlay texte dans Video:, Audio:, nommage
    perso/franchise, redondance, filler) exige une réécriture via AltSG : le
    shortener bouclerait en vain dessus (cf. run du 21/09 : '6 plans' renvoyé
    4x au shortener jusqu'à 'max reformat attempts exceeded').
    """
    low = (err or "").lower()
    return (
        "vo beaucoup trop longue" in low
        or "vo trop longue pour la durée de son plan" in low
    )


def is_asset_error(err: str) -> bool:
    """True si l'erreur cible une ligne `Video:` précise (préfixée "ASSET(plan N)").

    Ces erreurs sont réparables par le ScriptFixer — réécriture de la SEULE
    ligne Video fautive, structure du script intacte — au lieu d'une régénération
    complète via AltSG. C'est la réparation ciblée demandée (validation JEV +
    brackets + statique)."""
    low = (err or "").lower()
    return "asset(plan" in low


def is_timing_error(err: str) -> bool:
    """True si l'erreur porte sur les horaires d'un plan (déclarés ≠ Σ assets).

    Ces erreurs sont réparables par le filet déterministe repair_pacing_script
    (ajout d'une 2e vidéo puis raccourcissement de la VO, horaires réécrits) :
    la durée se CALCULE, elle ne s'improvise pas. Sans cette route elles
    tombaient dans le `else` → AltSG, et le run rebouclait (cf. run du 28/09 :
    7 reformat pour la même erreur d'horaires)."""
    low = (err or "").lower()
    return (
        "ne correspondent pas à la somme de ses assets" in low
        or "somme de ses assets" in low
        or "trop de vidéos au plan" in low
    )


_ALONE_TOKENS = (" alone", " alone.", " alone,", "lone", "solo")


class PydanticScriptValidationNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=1, wait=5)
        self.step_name = "pydantic_script_validation"

    async def prep_async(self, shared):
        shared["_current_step"] = self.step_name
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        script_data = {"script": shared.get("script", "")}
        try:
            GeneratedScript(**script_data)
            return {"valid": True}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    async def post_async(self, shared, prep, exec):
        if exec["valid"]:
            shared["_current_step"] = f"{self.step_name}_done"
            shared["steps"].append({
                "step": self.step_name,
                "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:200],
            })
            await _set_state(**_shared_snapshot(shared))
            return "default"

        error = exec["error"]
        shared["_reformat_error"] = error
        shared["_reformat_attempts"] = shared.get("_reformat_attempts", 0) + 1
        attempts = shared["_reformat_attempts"]

        shared["_current_step"] = f"{self.step_name}_reformat"
        shared["steps"].append({
            "step": self.step_name,
            "status": "reformat",
            "ts": datetime.now(timezone.utc).isoformat(),
            "error": error,
            "attempt": attempts,
        })
        await _set_state(**_shared_snapshot(shared))

        if attempts > 3:
            shared["_error"] = f"PydanticScriptValidation: max reformat attempts exceeded: {error}"
            return "error"

        return "reformat"


class AltPydanticScriptValidationNode(PydanticScriptValidationNode):
    """Version dédiée pour le chemin alternatif pour éviter le partage du stepid.
    Ajoute le filet déterministe : sur un échec pacing/timing récurrent (après la
    tentative GLM), répare le script mécaniquement (assets + horaires) au lieu de
    boucler jusqu'à la mort."""

    def __init__(self):
        super().__init__()
        self.step_name = "pydantic_script_validation_alt"

    async def post_async(self, shared, prep, exec):
        if exec["valid"]:
            shared["_current_step"] = f"{self.step_name}_done"
            shared["steps"].append({
                "step": self.step_name,
                "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:200],
            })
            await _set_state(**_shared_snapshot(shared))
            return "default"

        error = exec["error"]
        shared["_reformat_error"] = error
        shared["_reformat_attempts"] = shared.get("_reformat_attempts", 0) + 1
        attempts = shared["_reformat_attempts"]

        shared["_current_step"] = f"{self.step_name}_reformat"
        shared["steps"].append({
            "step": self.step_name,
            "status": "reformat",
            "ts": datetime.now(timezone.utc).isoformat(),
            "error": error,
            "attempt": attempts,
        })
        await _set_state(**_shared_snapshot(shared))

        # Filet déterministe : dès la 1re erreur de pacing/timing on répare
        # nous-mêmes (un beat trop long → max 2 assets, horaires = Σ assets)
        # au lieu de reboucler vers le SG. La durée se calcule, elle ne
        # s'improvise pas : aucun LLM n'est nécessaire.
        from nodes.scriptwriter.script_timing import pacing_errors, repair_pacing_script
        if is_timing_error(error) or (attempts > 1 and pacing_errors(shared.get("script", ""))):
            before = shared.get("script", "")
            repaired = repair_pacing_script(before, max_assets=2)
            if repaired is not None and repaired != before:
                # On valide sur le mandat du repair (pacing/timing) ET NON sur
                # GeneratedScriptAlt complet : sinon une règle de style sans
                # rapport (mot-clé, redondance) faisait jeter une réparation
                # parfaite et renvoyait tout le script au LLM (cf. run 28/09).
                remaining = pacing_errors(repaired)
                if remaining:
                    log.warning(
                        "PydanticScriptValidation ALT -> repair pacing incomplet, "
                        "on garde le LLM: %s", remaining[:2]
                    )
                    repaired = None
            if repaired is not None:
                shared["script"] = repaired
                shared["_reformat_error"] = ""
                shared["_current_step"] = f"{self.step_name}_auto_repair"
                shared["steps"].append({
                    "step": self.step_name,
                    "status": "auto_repair",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "output": "pacing/timing reparé mécaniquement (max 2 assets + horaires)",
                })
                await _set_state(**_shared_snapshot(shared))
                return "default"

        if attempts > 4:
            shared["_error"] = f"PydanticScriptValidation: max reformat attempts exceeded: {error}"
            return "error"

        # Routage sélectif :
        #  - VO trop longue → VoShortener (réécriture ciblée des VO, structure intacte) ;
        #  - Video: fautive (bracket/statique/headcount JEV) → ScriptFixer
        #    (réécriture de la SEULE ligne Video fautive — la réparation ciblée par
        #    asset demandée : quand un asset est rejeté, seul cet asset est réécrit) ;
        #  - horaires de plan incohérents → filet déterministe repair_pacing_script
        #    (aucun LLM : la durée se calcule) ;
        #  - toute autre erreur (plans, nommage, structure...) → AltSG
        #    (régénération complète, seul capable d'ajouter un plan ou réparer la structure).
        # Le garde-fou anti-boucle ne s'applique PAS aux cas ci-dessus, qui se
        # réparent sans régénérer : il ne compte que les vraies régénérations AltSG.
        if not is_vo_length_error(error) and not is_asset_error(error) \
                and not is_timing_error(error) \
                and shared.get("_sg_regens", 0) >= 6:
            shared["_error"] = (
                "PydanticScriptValidation: trop de régénérations AltSG "
                f"({shared.get('_sg_regens', 0)}): {error}"
            )
            return "error"
        if is_vo_length_error(error):
            route = "reformat_vo"
        elif is_asset_error(error):
            route = "reformat_video"
        elif is_timing_error(error):
            route = "reformat_timing"
        else:
            route = "reformat_sg"
        log.info(f"PydanticScriptValidation ALT -> {route} ({str(error)[:100]})")
        return route

    async def exec_async(self, shared):
        script_data = {"script": shared.get("script", "")}
        try:
            GeneratedScriptAlt(**script_data)
        except Exception as e:
            return {"valid": False, "error": str(e)}

        # Personnage + franchise doivent être NOMÉS oralement dans les VO (pas juste
        # dans le visuel) sinon le spectateur ne sait pas de quoi parle la vidéo.
        value = shared.get("script", "")
        character = (shared.get("selected_article") or {}).get("character") or {}
        name = (character.get("name") or "").strip()
        franchise = (character.get("franchise") or "").strip()
        if name:
            vo_text = "\n".join(
                l for l in value.splitlines()
                if re.match(r"^\s*[-*]?\s*VO\s*:", l, re.IGNORECASE)
            ).lower()
            # nom exact (insensible cas) + franchisation (insensible cas)
            if name.lower() not in vo_text:
                return {"valid": False, "error": (
                    f"Le personnage '{name}' n'est PAS NOMÉ dans les VO. La vidéo doit "
                    "dire oralement qui c'est dès le hook (VO), pas seulement le montrer "
                    "visuellement. Ajoute le nom dans au moins une VO (de préférence le hook)."
                )}
            if franchise and franchise.lower() not in vo_text:
                return {"valid": False, "error": (
                    f"La franchise/jeu '{franchise}' n'est PAS NOMÉ dans les VO. Un "
                    "spectateur lambda doit apprendre DE QUOI il s'agit. Nomme la "
                    "franchise dans au moins une VO."
                )}

        # JEV headcount par ligne Video: — validation BLoquante : si la ligne décrit
        # UNE personne mais n'écrit pas 'alone', LTX invente un second personnage →
        # c'est LE symptôme du slop TikTok générique (clip 2 du 22/09). JEV en panne
        # (ok=False) → skip, jamais de blocage (le run continue).
        from helpers.headcount_guard import classify_headcount

        # Découpe I2V / T2V — source de vérité unique (helpers.i2v_slots), la même
        # fonction que celle qui marque `mode: i2v` dans l'AssetPlanner. On collecte
        # d'abord les plans de chaque ligne Video: (et la VO du même plan) parce que
        # les deux contrôles JEV portent sur des sous-ensembles différents.
        plan_idx = None
        vo_by_plan = {}
        shots = []  # [(plan_index, texte Video:), ...] dans l'ordre du script
        for line in value.splitlines():
            m = re.match(r"^\s*[-*]?\s*Plan\s*(\d+)\s*\(", line)
            if m:
                plan_idx = int(m.group(1))
                continue
            if re.match(r"^\s*[-*]?\s*VO\s*:", line, re.IGNORECASE):
                vo_by_plan.setdefault(
                    plan_idx,
                    re.sub(r"^\s*[-*]?\s*VO\s*:\s*", "", line, flags=re.IGNORECASE),
                )
                continue
            if not re.match(r"^\s*[-*]?\s*Video\s*:", line, re.IGNORECASE):
                continue
            shots.append((plan_idx, re.sub(
                r"^\s*[-*]?\s*Video\s*:\s*", "", line, flags=re.IGNORECASE)))

        from helpers.i2v_slots import I2V_SLOT_COUNT, i2v_slot_positions
        i2v_pos = i2v_slot_positions([p for p, _ in shots], I2V_SLOT_COUNT)

        for plan_idx, text in shots:
            verdict = await classify_headcount(text)
            if not verdict.get("ok"):
                log.info(f"AltPydantic JEV -> JEV indisponible pour Video plan {plan_idx}, skip")
                continue
            choice = verdict.get("choice")
            conf = verdict.get("confidence", 0.0)
            if choice == "one_person" and conf >= 0.8:
                low = text.lower()
                if not any(t in low for t in _ALONE_TOKENS):
                    return {"valid": False, "error": (
                        f"ASSET(plan {plan_idx}): headcount JEV confirme UNE seule personne "
                        f"(conf {conf:.2f}) mais la Video: n'écrit pas 'alone'. Sans ce mot, "
                        "LTX invente un deuxième personnage de fond → vidéo générique. "
                        "Ajoute 'alone' (ou `lone`/`solo`) dans la description."
                    )}

        # JEV b-roll par ligne T2V — la règle éditoriale du run : les 2 premières
        # vidéos sont des I2V (le personnage doit y être filmé), TOUS les autres
        # plans sont du b-roll et ne doivent montrer PERSONNE de reconnaissable :
        # ils portent l'IDÉE de la VO du plan (décor, objet, mains en détail,
        # scène vide, foule de dos). C'était la cause du slop : la règle
        # "décris la physionomie dans CHAQUE Video:" forçait 6 plans sur 7 à
        # reparler de la même fille. Ici on ne touche QUE les T2V (positions
        # hors i2v_pos), jamais les ancrages I2V.
        # Violation → préfixe ASSET(plan N) → routage auto vers le VideoAssetFixer
        # (is_asset_error), qui réécrit la SEULE ligne fautive. JEV en panne → skip.
        from helpers.broll_guard import classify_broll
        for pos, (plan_idx, text) in enumerate(shots):
            if pos in i2v_pos:
                continue
            verdict = await classify_broll(
                text, vo_by_plan.get(plan_idx, ""), name, franchise)
            if not verdict.get("ok"):
                log.info(f"AltPydantic JEV b-roll -> JEV indisponible pour Video plan "
                         f"{plan_idx}, skip")
                continue
            choice = verdict.get("choice")
            conf = verdict.get("confidence", 0.0)
            if choice == "character_visible" and conf >= 0.8:
                who = name or "le personnage central"
                vo = (vo_by_plan.get(plan_idx, "") or "").strip()
                return {"valid": False, "error": (
                    f"ASSET(plan {plan_idx}): ce plan est un T2V (b-roll) mais la Video: "
                    f"montre {who} comme une personne visible (JEV conf {conf:.2f}). "
                    f"Règle : seules les {I2V_SLOT_COUNT} premières vidéos sont des I2V "
                    f"(personnage filmé) ; tous les autres plans doivent être des inserts "
                    f"qui portent l'IDÉE de la VO, sans le personnage. "
                    + (f"La VO de ce plan est « {vo[:120]} » : réécris la Video: comme un "
                       f"plan d'insert sur cette idée (décor, objet, mains en détail, "
                       f"scène vide, foule de dos, environnement)." if vo else
                       "Réécris la Video: comme un plan d'insert (décor, objet, mains "
                       "en détail, scène vide, foule de dos, environnement).")
                )}
        return {"valid": True}
