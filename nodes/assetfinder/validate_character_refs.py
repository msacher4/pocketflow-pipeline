import asyncio
import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import TG_VALIDATION_TIMEOUT
from helpers.state import _set_state, _shared_snapshot, _register_validation, _pending_validations
from nodes.assetfinder.validate_i2v import send_photo_tg

log = logging.getLogger("pocketflow-pipeline")


class ValidateCharacterRefs(AsyncNode):
    """Validation Telegram des RÉFÉRENCES D'IDENTITÉ, avant toute génération Klein.

    Jev-Omni ne sait dire que « un seul personnage ou pas » : c'est un modèle de
    décision, pas un reconnaisseur de franchise. La preuve que l'image montre bien
    LE personnage visé reste un jugement humain — donc on montre chaque référence
    avec son URL source et son verdict headcount, et on ne passe à Klein qu'après
    ton accord.

    ❌ sur une référence -> `reject_refs` avec son URL blacklistée, ce qui reboucle
    vers RealCharacterImageNode qui pioche le candidat suivant. Timeout -> approve.
    """

    step = "validate_character_refs"

    def __init__(self, timeout: int = TG_VALIDATION_TIMEOUT):
        super().__init__(max_retries=1, wait=5)
        self.timeout = timeout

    async def prep_async(self, shared):
        shared["_current_step"] = "validate_character_refs"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        refs = shared.get("character_refs") or []
        if not refs:
            log.info("ValidateCharacterRefs: aucune référence à valider")
            return json.dumps({"action": "approve", "rejected_urls": []}, ensure_ascii=False)

        character = (shared.get("asset_blueprint", {}).get("slots") or [{}])
        character = next(
            (s.get("character") or {} for s in character if isinstance(s, dict)), {}
        )
        pipeline_id = shared.get("pipeline_id", "unknown")

        sent = []
        for idx, ref in enumerate(refs):
            path = ref.get("path")
            if not path:
                continue
            vid = f"{pipeline_id}_pf_ref_{idx}"
            buttons = [[
                {"text": "✅ C'est le bon perso", "callback_data": f"approve:{vid}"},
                {"text": "❌ Mauvais perso", "callback_data": f"reject:{vid}"},
            ]]
            msg_id = await send_photo_tg(path, self._caption(ref, idx, character), buttons)
            if msg_id:
                sent.append((idx, ref, vid, _register_validation(vid)))

        if sent:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(e.wait() for _, _, _, e in sent)), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                log.info("ValidateCharacterRefs timeout, auto-approve")
                for _, _, vid, _ in sent:
                    _pending_validations.pop(vid, None)
                return json.dumps({"action": "approve", "rejected_urls": []}, ensure_ascii=False)

        rejected_urls = []
        for _, ref, vid, _ in sent:
            entry = _pending_validations.pop(vid, {})
            if not entry.get("result", "reject").startswith("approve"):
                if ref.get("url"):
                    rejected_urls.append(ref["url"])

        log.info(f"ValidateCharacterRefs: {len(rejected_urls)} référence(s) rejetée(s)")
        if not rejected_urls:
            return json.dumps({"action": "approve", "rejected_urls": []}, ensure_ascii=False)

        blacklisted = set(shared.get("_rejected_ref_urls") or []) | set(rejected_urls)
        shared["_rejected_ref_urls"] = sorted(blacklisted)
        return json.dumps({"action": "reject_refs", "rejected_urls": rejected_urls},
                          ensure_ascii=False)

    @staticmethod
    def _caption(ref: dict, idx: int, character: dict) -> str:
        who = character.get("name") or "?"
        work = character.get("franchise") or "?"
        head = ref.get("headcount") or {}
        if head.get("ok"):
            verdict = f"JEV headcount : {head.get('choice')} ({head.get('confidence', 0):.2f})"
        else:
            verdict = f"JEV headcount : indisponible ({head.get('reason', '?')})"
        return (
            f"Référence {idx + 1}/{ref.get('total', '?')} — {who} ({work})\n"
            f"{ref.get('w')}×{ref.get('h')} · source {ref.get('source', '?')}\n"
            f"{verdict}\n"
            f"C'est bien CE personnage ?\n{ref.get('url', '')}"
        )

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            data = {"action": "approve"}

        action = data.get("action", "approve")
        if action == "approve":
            for ref in shared.get("character_refs") or []:
                ref["confirmed"] = True
            shared.pop("_rejected_ref_urls", None)

        shared["steps"].append({
            "step": "validate_character_refs", "status": action,
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"rejet={len(data.get('rejected_urls', []))}",
        })
        shared["_current_step"] = "validate_character_refs_done"
        await _set_state(**_shared_snapshot(shared))
        return "default" if action == "approve" else action