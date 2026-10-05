import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pocketflow import AsyncNode

from helpers.state import _set_state, _shared_snapshot, _set_traces, _save_sub_shared
from helpers.icrawler_provider import build_query, search_character_images
from helpers.jev_omni import headcount_on_image

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"

# Nombre de références d'identité récupérées AU TOTAL, et non par slot. Elles sont
# PARTAGÉES par tous les slots i2v : c'était le bug initial, deux références par
# slot.times deux slots donnaient quatre images, et rien ne garantissait que les
# quatre montraient le même personnage — Klein partait alors sur deux personnes
# différentes. Une seule identité, partagée par tous les plans.
REF_COUNT_TOTAL = 2


def _url_key(url: str) -> str:
    """Identité stable d'une image, insensible aux paramètres d'URL.

    Bing réécrit ses URL à chaque crawl (jetons de cache, `?cb=`, `/revision/latest/`),
    donc comparer deux chaînes brutes laisse repasser une image déjà rejetée.
    """
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return (url or "").strip().lower()
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{(parts.path or '').rstrip('/').lower()}"

# Nombre de candidats downloaded avant de donner la main à Telegram. On en
# garderait bien plus s'il n'y avait pas la validation humaine.
MAX_CANDIDATES = 12


class RealCharacterImageNode(AsyncNode):
    """Récupère les images RÉELLES du personnage pour les références d'identité Klein.

    Ces images ne sont PAS des frames I2V : ce sont des références d'identité,
    consommées par ComfyUIKleinRefImageGenerator pour produire l'image de source
    réellement animée.

    Chaîne de confiance, du plus large au plus étroit :
    1. `icrawler` (Google puis repli Bing) cherche « nom + oeuvre ». Le nom seul est
       ambigu — c'est exactement ce qui a fait échouer Danbooru, où les tags
       `lilly_*` ramenaient des personnages homonymes sans rapport.
    2. Jev-Omni écarte les images qui montrent plusieurs personnages : il VOIT
       l'image (contrairement au JEV hébergé, text-only).
    3. ValidateCharacterRefs demande confirmation sur Telegram avant Klein.

    Les 2 références retenues sont communes à tous les slots i2v. Si on n'obtient
    pas 2 références, on ne fabrique pas de fallback : le run est abandonné et on
    relate vers l'ActionFinder (retry_no_image).
    """

    def __init__(self):
        super().__init__(max_retries=1, wait=5)

    async def prep_async(self, shared):
        shared["_current_step"] = "real_character_image"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        blueprint = shared.get("asset_blueprint", {})
        slots = blueprint.get("slots", [])
        i2v_slots = [s for s in slots if s.get("mode") == "i2v"]
        if not i2v_slots:
            return json.dumps({"generated_images": [], "missing_ids": []}, ensure_ascii=False)

        targets = shared.get("_i2v_regen_image_slots")
        if targets:
            i2v_slots = [s for s in i2v_slots if s.get("id") in targets]
            if not i2v_slots:
                return json.dumps({"generated_images": [], "missing_ids": []}, ensure_ascii=False)

        pipeline_id = shared.get("pipeline_id", "unknown")
        dest_dir = DOWNLOADS_DIR / pipeline_id / "real"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Les références étant partagées, c'est le personnage du premier slot i2v
        # qui fait foi pour tout le run.
        character = (i2v_slots[0].get("character") or {})
        if not character.get("name"):
            log.warning("RealCharacterImage: slot i2v sans nom de personnage")
            return json.dumps(
                {"generated_images": [], "missing_ids": [s.get("id") for s in i2v_slots]},
                ensure_ascii=False,
            )

        refs, checks = await self._fetch_refs(character, dest_dir, shared)
        self._publish_checks(character, refs, checks, build_query(character))
        if len(refs) < REF_COUNT_TOTAL:
            missing_ids = [s.get("id") for s in i2v_slots]
            log.warning(
                f"RealCharacterImage: {len(refs)} référence(s) pour "
                f"{character.get('name')} (il en faut {REF_COUNT_TOTAL}) -> retry_no_image"
            )
            return json.dumps({"generated_images": [], "missing_ids": missing_ids},
                              ensure_ascii=False)

        for ref in refs:
            ref["total"] = len(refs)
        shared["character_refs"] = refs
        self._write_manifest(pipeline_id, character, refs)

        generated = [{
            "slot_id": slot.get("id"),
            "prompt": slot.get("prompt", ""),
            "image_path": refs[0]["path"],
            "reference_paths": [r["path"] for r in refs],
            "source": "icrawler",
            "confirmed": True,
        } for slot in i2v_slots]

        return json.dumps({"generated_images": generated, "missing_ids": []},
                          ensure_ascii=False)

    async def _fetch_refs(self, character: dict, dest_dir: Path,
                          shared: dict) -> tuple[list[dict], list[dict]]:
        """Icropper -> filtre headcount Jev-Omni -> 2 références communes.

        Renvoie (refs, checks). `checks` note UN enregistrement par candidat,
        rejets compris : c'est ce qui s'affiche dans le popup Svelte. Avant, un
        rejet partait en `continue` sans être gardé, donc rien ne montrait
        jamais pourquoi un candidat avait été écarté.

        Une erreur Jev ne fait PAS rejeter le candidat : on le garde et on le signale,
        c'est la validation Telegram qui tranche. Rejeter sur une panne de modèle
        ferait perdre des images parfaitement bonnes.
        """
        query = build_query(character)

        # icrawler ne renvoie que ce qu'il télécharge réellement : re-crawler dans
        # le MÊME dossier ne ressort pas les images déjà là. Au 2e passage (après
        # un rejet Telegram) le pool était donc vide et le retry ne pouvait plus
        # rien trouver. Un dossier par tentative garantit un pool réellement neuf.
        attempt = int(shared.get("_ref_fetch_attempt") or 0)
        shared["_ref_fetch_attempt"] = attempt + 1
        cand_dir = dest_dir / "candidates" if attempt == 0 else dest_dir / f"candidates_r{attempt}"

        log.info(f"RealCharacterImage: recherche icrawler « {query} » (tentative {attempt + 1})")

        candidates = await asyncio.to_thread(
            search_character_images, query, str(cand_dir),
            MAX_CANDIDATES * 2, MAX_CANDIDATES,
        )
        if not candidates:
            log.warning(f"RealCharacterImage: aucun candidat pour « {query} »")
            return [], []

        blacklisted = {_url_key(u) for u in (shared.get("_rejected_ref_urls") or [])}
        if blacklisted:
            before = len(candidates)
            # Normalisation : une même image peut revenir avec une URL différente
            # (paramètres de cache Bing), donc on compare host+chemin, pas l'URL.
            candidates = [c for c in candidates if _url_key(c["url"]) not in blacklisted]
            log.info(f"RealCharacterImage: {before - len(candidates)} candidat(s) "
                     f"blacklisté(s) par la validation Telegram")

        refs: list[dict] = []
        checks: list[dict] = []
        for cand in candidates:
            if len(refs) >= REF_COUNT_TOTAL:
                break
            verdict = await headcount_on_image(cand["path"])
            ok = bool(verdict.get("ok"))
            checks.append({
                "file": Path(cand["path"]).name,
                "dimensions": [cand["w"], cand["h"]],
                "source": cand["source"],
                "url": cand["url"],
                "headcount": verdict.get("choice"),
                "confidence": verdict.get("confidence"),
                "verdict": verdict.get("verdict") if ok else "erreur",
                "error": None if ok else verdict.get("reason"),
                "accepted": not ok or verdict.get("verdict") != "reject",
            })
            # Seul un rejet EXPLICITE de Jev écarte le candidat. Une panne du modèle
            # (ok=False) le laisse passer : c'est alors Telegram qui tranche, sinon
            # un incident Jev ferait perdre des images parfaitement bonnes.
            if ok and verdict.get("verdict") == "reject":
                continue
            refs.append({
                "path": cand["path"],
                "url": cand["url"],
                "source": cand["source"],
                "w": cand["w"],
                "h": cand["h"],
                "headcount": verdict,
            })

        if refs:
            log.info(
                f"RealCharacterImage: {len(refs)} référence(s) d'identité pour "
                f"{character.get('name')} -> {[r['path'] for r in refs]}"
            )
        return refs, checks

    @staticmethod
    def _publish_checks(character: dict, refs: list[dict], checks: list[dict],
                        query: str) -> None:
        """Publie les verdicts Jev dans le sous-store, lus par le popup Svelte.

        Sans cet appel, le popup affiche « Aucune donnée pour cette étape » : c'est
        le symptôme signalé. La vue générique fait déjà fetchSubShared() sur ce
        stepId, donc aucune modification du front n'est nécessaire.
        """
        _save_sub_shared("real_character_image", {
            "character": character.get("name"),
            "query": query,
            "counters": {
                "candidates": len(checks),
                "accepted": sum(1 for c in checks if c["accepted"]),
                "rejected": sum(1 for c in checks
                                if not c["accepted"] and c["verdict"] == "reject"),
                "errors": sum(1 for c in checks if c["verdict"] == "erreur"),
                "refs_used": len(refs),
            },
            "checks": checks,
        })

    @staticmethod
    def _write_manifest(pipeline_id: str, character: dict, refs: list[dict]) -> None:
        """Trace les URLs sources des références.

        Sans ça on se retrouve avec des .img anonymes impossibles à diagnostiquer —
        c'est ce qui a rendu le run Danbooru 20261004_184955 opaque.
        """
        manifest = {
            "pipeline_id": pipeline_id,
            "character": {"name": character.get("name"), "franchise": character.get("franchise")},
            "refs": [
                {
                    "path": r["path"],
                    "url": r["url"],
                    "source": r["source"],
                    "dimensions": [r["w"], r["h"]],
                    "headcount": r.get("headcount", {}).get("choice"),
                    "headcount_confidence": r.get("headcount", {}).get("confidence"),
                }
                for r in refs
            ],
        }
        try:
            base = DOWNLOADS_DIR / pipeline_id
            base.mkdir(parents=True, exist_ok=True)
            (base / "character_refs.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            log.warning(f"RealCharacterImage: manifest non écrit: {e}")

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error("RealCharacterImage POST -> exec is not valid JSON")
            shared["_current_step"] = "real_character_image_error"
            shared["_error"] = "RealCharacterImage: exec is not valid JSON"
            shared["steps"].append({
                "step": "real_character_image", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("RealCharacterImage aborted: exec is not valid JSON")

        new_images = data.get("generated_images", [])
        missing_ids = set(data.get("missing_ids", []))
        prev = shared.get("generated_images", [])
        kept = [i for i in prev if i.get("slot_id") not in {g["slot_id"] for g in new_images}]
        shared["generated_images"] = kept + new_images

        refs = shared.get("character_refs") or []
        shared["steps"].append({
            "step": "real_character_image", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(refs)} référence(s) commune(s), {len(missing_ids)} slot(s) sans image",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))

        # Un slot I2V sans image (perso non trouvé) -> on ne bricole pas :
        # abandon du run, rebouclage ActuFinder pour un autre article.
        if missing_ids:
            log.warning(f"RealCharacterImage: slots I2V sans image {missing_ids} -> retry_no_image")
            shared["_missing_image_slots"] = sorted(missing_ids)
            return "retry_no_image"
        return "default"