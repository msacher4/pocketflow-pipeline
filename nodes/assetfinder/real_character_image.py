import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import asyncio
from pocketflow import AsyncNode

from config import LLM_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm
from helpers.danbooru import search_posts, download_image

log = logging.getLogger("pocketflow-pipeline")

DOWNLOADS_DIR = Path(__file__).parent.parent.parent / "downloads"


class RealCharacterImageNode(AsyncNode):
    """Recherche et télécharge les images RÉELLES des personnages (run alt),
    depuis Danbooru (rating:g, SFW exclusivement).

    Pour chaque slot du blueprint marqué `mode == "i2v"` :
    1. Recherche les posts Danbooru rating:g du personnage.
    2. Un LLM choisit le visuel le plus adapté (portrait 9:16, grande résolution).
    3. Télécharge l'image et l'écrit dans `generated_images` avec `confirmed: True`
       (validation automatique, skip Telegram).

    Si UN des slots i2v n'a pas d'image (perso trop récent / non taggé), on ne
    fabrique pas de fallback : le run est abandonné et on relate vers l'ActionFinder
    (retry_no_image) pour en sélectionner un autre article (décision 1.a).
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

        generated = []
        missing_ids = []
        used_urls: set[str] = set()
        for slot in i2v_slots:
            character = slot.get("character") or {}
            if not character.get("name"):
                missing_ids.append(slot.get("id"))
                continue
            path, file_url = await self._fetch_one(slot, character, dest_dir, shared, used_urls)
            if path:
                generated.append({
                    "slot_id": slot.get("id"),
                    "prompt": slot.get("prompt", ""),
                    "image_path": path,
                    "confirmed": True,
                })
                if file_url:
                    used_urls.add(file_url)
            else:
                missing_ids.append(slot.get("id"))

        return json.dumps({"generated_images": generated, "missing_ids": missing_ids},
                          ensure_ascii=False)

    async def _fetch_one(self, slot, character, dest_dir, shared, exclude_urls=frozenset()) -> tuple[str | None, str]:
        posts = await asyncio.to_thread(search_posts, character)
        if exclude_urls:
            posts = [p for p in posts if p.get("file_url") not in exclude_urls]
        if not posts:
            log.warning(f"Slot {slot.get('id')}: aucun post Danbooru pour "
                        f"{character.get('name')} ({character.get('franchise')})"
                        + (f" hors {len(exclude_urls)} déjà retenu(s)" if exclude_urls else ""))
            return None, ""

        ctx_lines = [
            f"Personnage recherché: {character.get('name')} "
            f"(oeuvre: {character.get('franchise', '')})",
            f"Section du plan: {slot.get('section', '')} (position {slot.get('position', 0)})",
            f"Visuel attendu pour ce plan: {slot.get('content') or slot.get('prompt', '')[:400]}",
            "Contraintes : image SAFE/pour tous publics, visuel reconnaissable du "
            "personnage, de préférence format portrait (hauteur > largeur) et grande "
            "résolution pour un fond de vidéo.",
        ]
        if exclude_urls:
            ctx_lines.append(
                "IMPORTANT : les images suivantes ont DEJA ETE retenues pour d'autres "
                f"plans. Choisis UN VISUEL OBLIGATOIREMENT DIFFERENT (file_url à ne "
                f"PAS réutiliser) :\n{json.dumps(sorted(exclude_urls), ensure_ascii=False)}"
            )
        ctx_lines.append(f"Candidats (posts) :\n{json.dumps(posts, ensure_ascii=False)[:6000]}")
        ctx = "\n".join(ctx_lines)

        soul = load_soul("choose_character_image")
        resp = await call_llm(LLM_MODEL, soul, ctx)
        _trace_llm(shared, "real_character_image_choose", "exec", LLM_MODEL, soul, ctx, resp)
        try:
            decision = _extract_json(resp)
        except Exception:
            decision = {}
        images = decision.get("images") if isinstance(decision.get("images"), list) else None
        candidates = []
        if images:
            candidates = [_c for _c in images if isinstance(_c, dict) and _c.get("file_url")]
            if exclude_urls:
                candidates = [_c for _c in candidates if _c.get("file_url") not in exclude_urls]
        if not candidates:
            candidates = [self._fallback_choice(posts, exclude_urls)] if self._fallback_choice(posts, exclude_urls) else []
        if not candidates:
            return None, ""

        dest = str(dest_dir / f"char_{slot.get('id')}.img")
        path = None
        file_url = ""
        for cand in candidates:
            path = await asyncio.to_thread(download_image, cand["file_url"], dest)
            if path:
                file_url = cand["file_url"]
                break
        if path:
            log.info(f"Slot {slot.get('id')}: image réelle Danbooru -> {path}")
        return path, file_url

    @staticmethod
    def _fallback_choice(posts, exclude_urls=frozenset()):
        """Choisit par défaut le post portrait (h > w) de plus grande surface, en
        excluant les URLs déjà utilisées par d'autres slots."""
        if not posts:
            return None
        if exclude_urls:
            posts = [p for p in posts if p.get("file_url") not in exclude_urls]
        if not posts:
            return None
        return sorted(
            [p for p in posts if p.get("h", 0) > p.get("w", 0)] or posts,
            key=lambda p: p.get("h", 0) * p.get("w", 0),
            reverse=True,
        )[0]

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

        shared["_current_step"] = "real_character_image_done"
        shared["steps"].append({
            "step": "real_character_image", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"{len(new_images)} image(s) réelle(s), {len(missing_ids)} manquante(s)",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))

        # Un slot I2V sans image (perso non trouvé) -> on ne bricole pas :
        # abandon du run, rebouclage ActuFinder pour un autre article.
        if missing_ids:
            log.warning(f"RealCharacterImage: slots I2V sans image {missing_ids} -> retry_no_image")
            return "retry_no_image"
        return "default"
