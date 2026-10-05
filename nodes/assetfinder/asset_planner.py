import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from pocketflow import AsyncNode
from pydantic import BaseModel, Field

from config import LLM_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, LLMJSONQuoteError

log = logging.getLogger("pocketflow-pipeline")

SFX_LIBRARY_FILE = Path(__file__).parent.parent.parent / "VFX" / "sfx_library.json"

# Nombre de slots i2v par script (les 2 premiers visuels). Les autres restent T2V.
I2V_SLOT_COUNT = 2

SFX_CATEGORY_INTENT = {
    "whooshes": "transition de plan, changement de rythme",
    "impacts": "coupe franche, moment fort (impact metal)",
    "risers": "montée de tension juste avant un cut",
    "pops": "surbrillance d'un mot-clé au moment du cut",
}

SECTION_RE = re.compile(r"^#{1,6}\s*\(?([^\)(]*)\s*\)?", re.IGNORECASE)
VIDEO_RE = re.compile(r"^\s*[-*]?\s*Video\s*:\s*(.+)$", re.IGNORECASE)
VO_RE = re.compile(r"^\s*[-*]?\s*VO\s*:\s*(.+)$", re.IGNORECASE)
AUDIO_RE = re.compile(r"^\s*[-*]?\s*Audio\s*:\s*(.+)$", re.IGNORECASE)
PLAN_RE = re.compile(r"^\s*[-*]?\s*Plan\s*(\d+)\s*\(", re.IGNORECASE)

SECTION_NAMES = {
    "hook": "hook",
    "intro": "hook",
    "body": "body",
    "cta": "cta",
    "outro": "cta",
    "end": "cta",
}


def _norm_section(raw: str) -> str:
    key = raw.strip().lower()
    key = key.split("(")[0].strip()
    for s, v in SECTION_NAMES.items():
        if s in key:
            return v
    return "body"


def _load_sfx_library() -> list[dict]:
    """Charge la bibliothèque VFX locité (VFX/sfx_library.json) et ne garde que
    les fichiers réellement présents sur disque. Vide si absente/invalide."""
    try:
        data = json.loads(SFX_LIBRARY_FILE.read_text())
    except (OSError, ValueError):
        log.warning(f"AssetPlanner: bibliothèque SFX introuvable/invalide ({SFX_LIBRARY_FILE})")
        return []
    if not isinstance(data, list):
        return []
    catalog = []
    for e in data:
        if not isinstance(e, dict) or not e.get("file"):
            continue
        p = SFX_LIBRARY_FILE.parent / e["file"]
        if not p.is_file():
            continue
        catalog.append({
            "file": e["file"],
            "category": e.get("category", ""),
            "duration_s": e.get("duration_s"),
        })
    return catalog


def _transition_anchors(slots: list[dict]) -> list[dict]:
    """Plans visuels après le premier : ancres de transition.

    Une coupe existe ENTRE deux plans ; elle est reçue par le plan qui suit.
    Le premier plan n'a aucune transition d'entrée => jamais de SFX dessus."""
    ordered = sorted(slots, key=lambda s: s.get("position", 0))
    return [
        {
            "anchor_index": i,
            "position": s.get("position", 0),
            "section": s.get("section", ""),
            "content": (s.get("content") or "")[:80],
        }
        for i, s in enumerate(ordered)
        if i >= 1
    ]


def _select_sfx(decision_sfx, catalog: list[dict], anchors: list[dict]) -> list[dict]:
    """Valide la liste `sfx` choisie par le LLM — en ASSAINISSANT, jamais en
    levant : les SFX sont optionnels par design (`[]` = cas normal), tuer un run
    de 25 min sur un bruitage mal placé est disproportionné (cf. run du 21/09 :
    anchor_index 10 inexistant → run mort).

    Garanties en code (jamais laissées au LLM) :
    - anchor_index uniquement sur une transition existante (plan >= 1 et < n),
      jamais le premier plan (0) ni une ancre inexistante → sinon entrée écartée ;
    - fichiers uniquement ceux du catalogue VFX présent sur disque → sinon écarté ;
    - au plus un SFX par transition → doublon écarté (on garde le premier).
    Chaque écart est loggé en warning. Retourne [] si rien de valide.
    """
    if not isinstance(decision_sfx, list):
        log.warning("AssetPlanner SFX -> 'sfx' n'est pas une liste, ignoré (sfx=[])")
        return []
    valid_indexes = {a["anchor_index"] for a in anchors}
    catalog_files = {c["file"] for c in catalog}
    seen = set()
    out = []
    for item in decision_sfx:
        if not isinstance(item, dict) or not item.get("file"):
            log.warning(f"AssetPlanner SFX -> entrée invalide écartée (pas d'objet/file): {str(item)[:100]}")
            continue
        ai = item.get("anchor_index")
        f = item["file"]
        if ai not in valid_indexes:
            log.warning(
                f"AssetPlanner SFX -> ancre {ai} écartée : hors des transitions "
                f"existantes {sorted(valid_indexes)} (ni plan 0, ni ancre inexistante)."
            )
            continue
        if ai in seen:
            log.warning(f"AssetPlanner SFX -> doublon sur l'ancre {ai} écarté (1 max par transition)")
            continue
        if f not in catalog_files:
            log.warning(f"AssetPlanner SFX -> fichier '{f}' hors catalogue, entrée écartée")
            continue
        seen.add(ai)
        category = next(c["category"] for c in catalog if c["file"] == f)
        out.append({
            "anchor_index": ai,
            "label": item.get("label", f),
            "file": f,
            "category": category,
        })
    return out


def parse_script_assets(script: str) -> dict:
    """Parse le script et extrait la liste exacte des assets déclarés.

    Règles :
    - Ligne `Video:` -> slot visuel
    - Ligne `VO:` -> slot voiceover
    - Ligne `Audio:` -> slot musique
    - Ligne `SFX:` -> ignorée (les effets sonores seront gérés au montage
      à partir de la bibliothèque VFX, pas pendant la génération d'assets)
    - En-têtes `# HOOK` / `# BODY` / `# CTA` -> section courante
    - Positions attribuées dans l'ordre global d'apparition
    - Un plan sans ligne dédiée n'est pas un asset
    """
    slots, audio, voiceover = [], [], []
    section = "body"
    pos = 0
    vid_id = 0
    aud_id = 0
    vo_id = 0
    plan_index = None

    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = SECTION_RE.match(raw_line)
        if m and m.group(1).strip():
            section = _norm_section(m.group(1))
            continue

        m = PLAN_RE.match(raw_line)
        if m:
            plan_index = int(m.group(1))
            continue

        m = VIDEO_RE.match(raw_line)
        if m:
            vid_id += 1
            slots.append({
                "id": vid_id,
                "section": section,
                "position": pos,
                "plan_index": plan_index,
                "type": "visual",
                "content": m.group(1).strip(),
            })
            pos += 1
            continue

        m = AUDIO_RE.match(raw_line)
        if m:
            aud_id += 1
            audio.append({
                "id": f"a{aud_id}",
                "section": section,
                "position": pos,
                "type": "audio",
                "content": m.group(1).strip(),
            })
            pos += 1
            continue

        m = VO_RE.match(raw_line)
        if m:
            vo_id += 1
            voiceover.append({
                "id": f"v{vo_id}",
                "section": section,
                "position": pos,
                "type": "voiceover",
                "text": m.group(1).strip(),
            })
            pos += 1
            continue

    return {
        "slots": slots,
        "audio": audio,
        "voiceover": voiceover,
    }


class AssetPlannerNode(AsyncNode):
    SOUL = "asset_planner"

    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "asset_planner"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul(self.SOUL)
        parsed = parse_script_assets(shared.get("script", ""))
        if not any((parsed.get("slots"), parsed.get("audio"), parsed.get("voiceover"))):
            raise RuntimeError("AssetPlanner: aucun asset déclaré dans le script")

        anchors = _transition_anchors(parsed.get("slots", []))
        sfx_library = _load_sfx_library()

        ctx = (
            f"Génère les prompts pour les assets suivants (thème: {shared.get('topic', '')}):\n"
            f"{json.dumps(parsed, ensure_ascii=False)[:8000]}\n\n"
            f"Timeline des plans (un SFX se place UNIQUEMENT à une transition de plan, "
            f"c'est-à-dire au début d'un plan SUIVANT ; jamais sur le premier plan index 0) :\n"
            f"{json.dumps(anchors, ensure_ascii=False)}\n\n"
        )
        if sfx_library:
            ctx += (
                f"Catalogue SFX local (choisis UNIQUEMENT parmi ces fichiers) :\n"
                f"{json.dumps(sfx_library, ensure_ascii=False)}\n"
                f"Intention par catégorie : {json.dumps(SFX_CATEGORY_INTENT, ensure_ascii=False)}\n\n"
            )
        else:
            ctx += "Aucun SFX disponible : ne retourne PAS de champ 'sfx'.\n\n"
        ctx += (
            f"Retourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Structure : {{\"slots\": [...], \"audio\": [...], \"voiceover\": [...], "
            f"\"sfx\": [{{\"anchor_index\": 2, \"label\": \"bref pourquoi\", "
            f"\"file\": \"sfx_cut/...\"}}]}} "
            f"où 'sfx' est OPTIONNEL ([] ou absent si inutile, au plus 1 SFX par transition)."
        )
        retry_hint = shared.get("_llm_retry_hint", "")
        if retry_hint:
            ctx += f"\n--- REPONSE PRECEDENTE INVALIDE ---\n{retry_hint}\n"
        resp = await call_llm(LLM_MODEL, soul, ctx, max_tokens=16384, timeout=600)
        _trace_llm(shared, "asset_planner", "exec", LLM_MODEL, soul, ctx, resp)
        try:
            decision = _extract_json(resp)
            if not isinstance(decision.get("slots"), list):
                raise ValueError("slots must be a list")
            if not isinstance(decision.get("audio"), list):
                raise ValueError("audio must be a list")
            if not isinstance(decision.get("voiceover"), list):
                raise ValueError("voiceover must be a list")
        except LLMJSONQuoteError as e:
            log.error(f"AssetPlanner LLMJSONQuoteError — réponse LLM brute (len={len(resp)}):\n{resp}")
            shared["_llm_retry_hint"] = (
                "Ta réponse contenait des guillemets doubles non échappés "
                "dans une valeur string. Remplace TOUS les guillemets doubles "
                "par des apostrophes simples (')."
            )
            raise
        except ValueError as e:
            shared["_llm_retry_hint"] = f"JSON invalide : {e}"
            raise

        expected_slots = len(parsed.get("slots", []))
        got_slots = len(decision.get("slots", []))
        expected_audio = len(parsed.get("audio", []))
        got_audio = len(decision.get("audio", []))
        expected_vo = len(parsed.get("voiceover", []))
        got_vo = len(decision.get("voiceover", []))
        if (got_slots != expected_slots or got_audio != expected_audio
                or got_vo != expected_vo):
            # Le script est la source de vérité : on réconcilie au lieu de mourir.
            # (Avant : raise Slot count mismatch → run tué, ex. 14 vs 12 le 21/09.)
            log.warning(
                f"AssetPlanner -> compte différent du script "
                f"({got_slots}/{expected_slots} slots, {got_audio}/{expected_audio} "
                f"audio, {got_vo}/{expected_vo} vo) : réconciliation auto.")
            decision = await self._reconcile_assets(shared, soul, decision, parsed)
        if not anchors:
            decision["sfx"] = []
        else:
            try:
                decision["sfx"] = _select_sfx(decision.get("sfx", []), sfx_library, anchors)
            except ValueError as e:
                shared["_llm_retry_hint"] = f"SFX invalide : {e}"
                raise
        shared["_llm_retry_hint"] = ""
        log.info(f"AssetPlanner -> {len(decision.get('slots', []))} visual slots, {len(decision.get('audio', []))} audio slots, {len(decision.get('voiceover', []))} voiceover, {len(decision.get('sfx', []))} sfx")
        return json.dumps(decision, ensure_ascii=False)

    def _fallback_item(self, item: dict, kind: str) -> dict:
        """Prompt de secours construit depuis le contenu du script lui-même
        (zéro LLM). Moins 'travaillé' qu'un prompt LLM, mais l'asset existe et
        le run continue — au lieu de mourir sur un mismatch."""
        out = dict(item)
        if kind == "slots":
            content = (item.get("content") or "").strip()
            out.setdefault(
                "prompt",
                f"{content}, cinematic, dynamic camera movement, 9:16 portrait"
                if content else "cinematic dynamic shot, 9:16 portrait")
            out.setdefault("negative_prompt",
                           "blurry, low quality, text, watermark, deformed")
            out.setdefault("expected", content[:200])
        elif kind == "audio":
            out.setdefault("mood", "upbeat anime battle theme")
        elif kind == "voiceover":
            out.setdefault("temperature", 0.7)
        return out

    async def _fetch_missing_prompts(self, shared, soul, missing: dict) -> dict:
        """Mini-appel LLM ciblé UNIQUEMENT sur les assets manquants (contexte
        minuscule → quasi garanti). Retourne {} en cas d'échec : le fallback
        déterministe prendra le relais."""
        try:
            ctx = (
                "Certains assets n'ont pas reçu leurs prompts. Génère les prompts "
                "UNIQUEMENT pour les assets suivants (garde exactement les mêmes "
                "ids) :\n"
                f"{json.dumps(missing, ensure_ascii=False)[:4000]}\n\n"
                "Retourne UNIQUEMENT un JSON valide {\"slots\": [...], "
                "\"audio\": [...], \"voiceover\": [...]} avec les mêmes ids. "
                "N'écris JAMAIS de guillemet double \" à l'intérieur d'une valeur."
            )
            resp = await call_llm(LLM_MODEL, soul, ctx, max_tokens=4096, timeout=600)
            _trace_llm(shared, "asset_planner", "exec_missing",
                        LLM_MODEL, soul, ctx, resp)
            fetched = _extract_json(resp)
            return fetched if isinstance(fetched, dict) else {}
        except Exception as e:
            log.warning(f"AssetPlanner -> mini-appel manquants échoué ({e}) : fallback.")
            return {}

    async def _reconcile_assets(self, shared, soul, decision: dict, parsed: dict) -> dict:
        """Aligne la réponse LLM sur le script (source de vérité), dans l'ordre
        du script :
        - ids inconnus/extras → écartés ;
        - ids manquants → mini-appel LLM ciblé, puis fallback déterministe ;
        - ne lève JAMAIS pour un mismatch de compte.
        """
        out = dict(decision)
        stats = {}
        for kind in ("slots", "audio", "voiceover"):
            parsed_items = parsed.get(kind, []) or []
            returned = decision.get(kind, []) or []
            by_id = {it.get("id"): it for it in parsed_items
                     if isinstance(it, dict)}
            enriched: dict = {}
            orphans = []
            for r in returned:
                if not isinstance(r, dict):
                    continue
                rid = r.get("id")
                if rid in by_id:
                    enriched[rid] = r
                else:
                    orphans.append(r)
            missing_ids = [it["id"] for it in parsed_items
                           if it["id"] not in enriched]
            n_orphan = n_mini = n_fallback = 0
            # Orphelins (slots sans id reconnu) réassignés aux ids manquants,
            # dans l'ordre — ce sont des prompts LLM exploitables.
            for mid in list(missing_ids):
                if not orphans:
                    break
                orph = orphans.pop(0)
                orph["id"] = mid
                enriched[mid] = orph
                missing_ids.remove(mid)
                n_orphan += 1
            # Mini-appel ciblé sur les ids toujours manquants.
            if missing_ids:
                missing_parsed = {"slots": [], "audio": [], "voiceover": []}
                missing_parsed[kind] = [by_id[mid] for mid in missing_ids]
                fetched = await self._fetch_missing_prompts(
                    shared, soul, missing_parsed)
                for r in (fetched.get(kind, []) or []):
                    if isinstance(r, dict) and r.get("id") in missing_ids:
                        enriched[r["id"]] = r
                        missing_ids.remove(r["id"])
                        n_mini += 1
            # Fallback déterministe pour les irréductibles.
            for mid in missing_ids:
                enriched[mid] = self._fallback_item(by_id[mid], kind)
                n_fallback += 1
            out[kind] = [enriched[it["id"]] for it in parsed_items]
            stats[kind] = (f"{len(returned)} reçus, {len(out[kind])} alignés "
                           f"(orphelins={n_orphan}, mini-llm={n_mini}, "
                           f"fallback={n_fallback})")
        log.info(f"AssetPlanner -> réconciliation : {stats}")
        return out

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("AssetPlanner POST -> exec is not valid JSON")
            shared["_current_step"] = "asset_planner_error"
            shared["_error"] = "AssetPlanner: exec is not valid JSON"
            shared["steps"].append({
                "step": "asset_planner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("AssetPlanner aborted: exec is not valid JSON")

        slots = decision.get("slots", [])
        audio = decision.get("audio", [])
        if not slots and not audio:
            log.warning("AssetPlanner POST -> blueprint is empty")
            shared["_current_step"] = "asset_planner_error"
            shared["_error"] = "AssetPlanner: empty blueprint"
            shared["steps"].append({
                "step": "asset_planner", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("AssetPlanner aborted: empty blueprint")

        shared["asset_blueprint"] = decision
        self._stamp_plan_index(shared, decision)
        self._decorate_blueprint(shared, decision)
        shared["_current_step"] = "asset_planner_done"
        shared["steps"].append({
            "step": "asset_planner", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("script", "")[:500],
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"

    def _decorate_blueprint(self, shared, decision):
        """Hook d'extension : no-op pour le chemin normal. Surchargé par
        AssetPlannerAltNode pour marquer les assets I2V (1re Video du Plan 1
        et 1re Video du Plan 2)."""
        return

    def _stamp_plan_index(self, shared, decision):
        """Reporte le plan_index (numéro du Plan) sur les slots visuels du
        blueprint, dans l'ordre du script. Déterministe, utilisé par le montage
        pour grouper les segments d'un plan multi-assets."""
        parsed = parse_script_assets(shared.get("script", ""))
        order = [s.get("plan_index") for s in parsed.get("slots", [])]
        visual = [s for s in decision.get("slots", []) if s.get("type") == "visual"]
        for s, pi in zip(visual, order):
            if pi is not None:
                s["plan_index"] = pi


class AssetPlannerAltNode(AssetPlannerNode):
    """Version alt : même job qu'AssetPlanner, mais BUILDING 100% DÉTERMINISTE.

    Plus aucun appel LLM (décision 22/09 : AssetPlanner reformulait les `Video:`
    de SW et PERDAIT le décor → slop TikTok générique). Le soul SW InfoMissedGen
    écrit désormais des prompts LTX-2.5-ready directement (règle 6) ; ici on fait
    `prompt = content` VERBATIM (brackets strippés), jamais de reformulation.

    Marque toujours les 2 premiers slots visuels comme I2V (1re Video du Plan 1
    et du Plan 2) et oriente la musique vers un mood péchu anime-JRPG."""

    SOUL = "asset_planner_alt"

    # Mapping déterministe mood musical ← mot-clé Audio: du script (règles du
    # soul) : la cible ALT veut du péchu anime/JRPG battle, jamais sombre/slow.
    _AUDIO_MOOD = {
        "upbeat": "upbeat anime battle theme",
        "anime": "energetic shonen opening",
        "j rpg": "heroic JRPG battle theme",
        "jrpg": "heroic JRPG battle theme",
        "game": "upbeat video-game theme",
        "gaming": "upbeat video-game theme",
        "positive": "bright uplifting video-game theme",
        "bright": "bright uplifting video-game theme",
        "epic": "epic anime battle anthem",
        "heroic": "heroic video-game drop",
    }
    _AUDIO_MOOD_DEFAULT = "upbeat anime battle theme"

    async def exec_async(self, shared):
        """Builder déterministe — ZÉRO LLM (le soul SW a déjà écrit les prompts
        LTX-ready ; toute reformulation ici perd le décor)."""
        parsed = parse_script_assets(shared.get("script", ""))
        if not any((parsed.get("slots"), parsed.get("audio"), parsed.get("voiceover"))):
            raise RuntimeError("AssetPlanner: aucun asset déclaré dans le script")

        slots = parsed.get("slots", []) or []
        audio = parsed.get("audio", []) or []
        voiceover = parsed.get("voiceover", []) or []

        def _clean(prompt: str) -> str:
            return re.sub(r"\s*\[\s*\d+\s*[-–]\s*\d+\s*s?\]\s*", " ", prompt)

        for s in slots:
            content = (s.get("content") or "").strip()
            s["prompt"] = _clean(content)
            s["expected"] = (content or "")[:600]
            s["negative_prompt"] = (
                "blurry, low quality, text, watermark, deformed, "
                "indoor apartment, selfie, casual home video, static room"
            )

        for a in audio:
            content = (a.get("content") or "").strip()
            a["mood"] = self._audio_mood_for(content)

        for v in voiceover:
            v["temperature"] = 0.7

        # SFX : JEV choisit DANS QUELLES transitions placer un effet (décision
        # sémantique), puis on résout les fichiers DÉTERMINISTIQUEMENT dans le
        # catalogue (jamais laissé au LLM). JEV indisponible → []. Jamais de crash.
        catalog = _load_sfx_library()
        anchors = _transition_anchors(slots)
        sfx = []
        if anchors and catalog:
            try:
                from helpers.headcount_guard import choose_sfx_across_transitions
                chosen = await choose_sfx_across_transitions(anchors, catalog)
                for a in anchors:
                    if a["anchor_index"] in chosen:
                        item = self._sfx_file_for(a, catalog)
                        if item:
                            sfx.append(item)
            except Exception as e:
                log.warning(f"AssetPlanner -> JEV SFX choisie échouée ({e}), sfx=[]")
                sfx = []
        decision = {
            "slots": slots,
            "audio": audio,
            "voiceover": voiceover,
            "sfx": sfx,
        }
        log.info(
            f"AssetPlanner(déterministe) -> {len(slots)} visual slots, "
            f"{len(audio)} audio, {len(voiceover)} voiceover, {len(sfx)} sfx"
        )
        return json.dumps(decision, ensure_ascii=False)

    def _audio_mood_for(self, content: str) -> str:
        low = (content or "").lower()
        for key, mood in self._AUDIO_MOOD.items():
            if key in low:
                return mood
        return self._AUDIO_MOOD_DEFAULT

    def _sfx_file_for(self, anchor: dict, catalog: list[dict]) -> dict | None:
        """Résolution DÉTERMINISTE du fichier SFX pour une transition choisie par
        JEV : section hook/body → whoosh ; cta → impact/riser ; sinon premier
        whoosh du catalogue. Fichier = 1er de la catégorie (ordre du catalogue).
        Écarté (None) si le fichier n'est pas sur disque ou la catégorie absente."""
        section = anchor.get("section", "")
        pref = ["impacts", "risers"] if section == "cta" else ["whooshes"]
        for cat in pref:
            for e in catalog:
                if e.get("category") == cat and e.get("file"):
                    return {
                        "anchor_index": anchor.get("anchor_index"),
                        "label": e.get("category", cat),
                        "file": e["file"],
                        "category": e.get("category", cat),
                    }
        return None

    def _decorate_blueprint(self, shared, decision):
        character = (shared.get("selected_article", {}) or {}).get("character") or {}
        slots = decision.get("slots", [])
        visual_slots = [s for s in slots if s.get("type") == "visual"]
        # Il n'y a QUE 2 I2V par script, tous les autres assets sont des T2V.
        # On marque les 2 PREMIERS slots visuels EXISTANTS, et non « le plan 1 et
        # le plan 2 » : un script dont le Plan 1 n'a pas de ligne Video: (hook
        # purement texte) n'a aucun slot pour le plan 1, et la règle par
        # plan_index n'en marquait qu'un seul -> Klein ne recevait qu'une image.
        for s in visual_slots[:I2V_SLOT_COUNT]:
            s["mode"] = "i2v"
            if character:
                s["character"] = character
        marked = sum(1 for s in visual_slots if s.get("mode") == "i2v")
        log.info(f"AssetPlannerAlt -> {marked}/{I2V_SLOT_COUNT} asset(s) I2V marqué(s) "
                 f"sur {len(visual_slots)} slot(s) visuel(s)")