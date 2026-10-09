import asyncio
import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import TG_BOT_TOKEN, TG_VALIDATION_TIMEOUT, LLM_MODEL
from helpers.state import (
    _set_state, _shared_snapshot, _set_traces,
    _register_validation, _pending_validations, _attach_message,
)
from helpers.send_telegram import send_telegram
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm

log = logging.getLogger("pocketflow-pipeline")

_SLOT_LABELS = {
    1: "Clip vidéo 1", 2: "Clip vidéo 2", 3: "Clip vidéo 3",
    "a1": "Musique 1", "a2": "Musique 2",
    "v1": "Voix off 1", "v2": "Voix off 2",
}

_SLOT_TO_TARGET = {
    1: "sdcpp_video_gen", 2: "sdcpp_video_gen", 3: "sdcpp_video_gen",
    "a1": "music_generator", "a2": "music_generator",
    "v1": "voice_generator", "v2": "voice_generator",
}

_SLOT_TO_TYPE = {
    1: "video", 2: "video", 3: "video",
    "a1": "music", "a2": "music",
    "v1": "voiceover", "v2": "voiceover",
}


class FeedbackInterpreterNode(AsyncNode):
    """On reject: send one feedback message for the current slot (ou pour la
    proposition entière quand aucun slot n'est rejeté), interpret via LLM and
    route to the rework target."""

    def __init__(self, step_name: str, format_proposal: callable, default_target: str, allowed_targets: list[str] | None = None, feedback_key: str = "script_feedback"):
        super().__init__(max_retries=1, wait=5)
        self.step_name = step_name
        self.format_proposal = format_proposal
        self.default_target = default_target
        self.allowed_targets = allowed_targets or []
        self.feedback_key = feedback_key
        self.timeout = TG_VALIDATION_TIMEOUT

    async def prep_async(self, shared):
        shared["_current_step"] = f"{self.step_name}_feedback"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        base_vid = f"{shared.get('pipeline_id', 'unknown')}_{self.step_name}"

        if not TG_BOT_TOKEN:
            log.info("[tg] SKIP feedback (no token)")
            return json.dumps({"decision": "rework", "target": self.default_target, "instructions": "", "raw_text": ""})

        current_slot = shared.get("_rejected_slot")
        current_label = shared.get("_rejected_slot_label", str(current_slot))
        remaining = shared.get("_pending_reject_queue", [current_slot])
        remaining_count = max(len(remaining), 1)
        queue_info = f" ({remaining_count} restant(s) après celui-ci)" if remaining_count > 1 else ""

        video_action = shared.get("_video_action", "regen")

        async def _ask_tg(suffix: str, text: str) -> str | None:
            """Pose une question Telegram et retourne la réponse, ou None (timeout/réponse invalide)."""
            q_vid = f"{base_vid}_{current_slot}_{suffix}"
            event = _register_validation(q_vid)
            msg_id = await send_telegram(text)
            if msg_id:
                _attach_message(q_vid, msg_id)
            try:
                await asyncio.wait_for(event.wait(), timeout=self.timeout)
            except asyncio.TimeoutError:
                _pending_validations.pop(q_vid, None)
                log.info(f"Feedback [{suffix}] timeout for slot {current_slot}")
                return None
            entry = _pending_validations.pop(q_vid, {})
            result = entry.get("result", "")
            if not result.startswith("feedback:"):
                log.info(f"Feedback [{suffix}] resolved with non-feedback result: {result}")
                return None
            answer = result[len("feedback:"):]
            log.info(f"Feedback [{suffix}] received for slot {current_slot}: {answer[:200]}")
            return answer

        # ---- Slot rejeté + action 'regen'/'i2v' : régénération ciblée ----
        if current_slot and video_action == "regen":
            feedback_text = await _ask_tg(
                "fb",
                f"✍️ Tu as choisi régénérer « {current_label} »{queue_info}.\n"
                "Réponds à ce message avec ton feedback "
                "(ex: \"le clip est flou\", \"le verre bouillonne\", \"change le ton\").",
            )
            if feedback_text is None:
                return json.dumps({"decision": "rework", "target": "sdcpp_video_gen", "instructions": "", "raw_text": ""})
            shared["af_feedback"] = feedback_text
            shared["_slots_to_regenerate"] = [current_slot]
            return json.dumps({"decision": "rework", "target": "sdcpp_video_gen", "instructions": feedback_text, "raw_text": feedback_text})

        if current_slot and video_action == "i2v":
            image_prompt = await _ask_tg(
                "img",
                f"🎨 Régénération i2v de « {current_label} »{queue_info}.\n"
                "Décris l'IMAGE à générer pour ce slot (elle sera ensuite animée en vidéo) :\n"
                "(ex: \"un smoothie vert dans un verre en verre sur un fond clair\")",
            )
            if image_prompt is not None and image_prompt.strip():
                shared["_i2v_image_prompt"] = image_prompt.strip()

            shared["_slots_to_regenerate"] = [current_slot]
            shared["_video_action"] = "i2v"
            shared["_i2v_phase"] = "image"
            return json.dumps({"decision": "rework", "target": "i2v", "instructions": "", "raw_text": ""})

        # ---- Feedback générique : slot rejeté (sans action ciblée) OU proposition
        #      entière rejetée sans slot (script, news) — on POSE TOUJOURS la
        #      question au lieu de régénérer en silence. ----
        proposal = self.format_proposal(shared)
        if not current_slot:
            current_label = "la proposition"

        target_hint = " | ".join(self.allowed_targets) if self.allowed_targets else self.default_target
        feedback_text = await _ask_tg(
            "fb",
            f"✍️ « {current_label} » rejetée{queue_info}.\n"
            "Réponds à ce message avec tes corrections : de quoi l'assemblage doit-il tenir compte "
            f"(target possible: {target_hint}) ?\n\n"
            f"Contexte de la proposition :\n{proposal[:600]}",
        )
        if not feedback_text:
            return json.dumps({"decision": "rework", "target": self.default_target, "instructions": "", "raw_text": ""})

        soul = load_soul("feedback_interpreter")
        slot_type = _SLOT_TO_TYPE.get(current_slot, "unknown")
        type_label = slot_id_type_label(slot_type)

        ctx = (
            f"Proposition rejetée: {current_label} (type: {type_label})\n\n"
            f"Proposition actuelle:\n{proposal}\n\n"
            f"Feedback de l'utilisateur:\n{feedback_text}\n\n"
            f"Interprète le feedback et retourne UNIQUEMENT un JSON valide:\n"
            f'{{"decision": "approve" ou "rework", '
            f'"target": "{target_hint}", '
            f'"instructions": "résumé concis des corrections à apporter"}}\n'
            f"Le target = le point de reprise du flow, voir ton soul pour les règles.\n"
            f"Le target doit correspondre au type de l'asset rejeté:\n"
            f"  - clip vidéo → sdcpp_video_gen\n"
            f"  - musique → music_generator\n"
            f"  - voix off → voice_generator\n"
            f"Si le feedback dit que tout est bon → decision=approve.\n"
            f"Si le feedback exige des modifications → decision=rework."
        )
        resp = await call_llm(LLM_MODEL, soul, ctx)
        _trace_llm(shared, f"{self.step_name}_feedback_interpreter", "exec", LLM_MODEL, soul, ctx, resp)
        decision = _extract_json(resp)
        decision["raw_text"] = feedback_text

        if decision.get("decision") == "approve":
            return json.dumps({"decision": "approve", "target": None, "instructions": "", "raw_text": feedback_text})

        target = decision.get("target", self.default_target)
        if self.allowed_targets and target not in self.allowed_targets:
            target = _SLOT_TO_TARGET.get(current_slot, self.default_target)
            decision["target"] = target

        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error(f"FeedbackInterpreter {self.step_name}: invalid JSON")
            shared[self.feedback_key] = ""
            shared["user_feedback"] = {"target": self.default_target, "instructions": "", "raw_text": ""}
            shared["_current_step"] = f"{self.step_name}_feedback_done"
            shared["steps"].append({
                "step": f"{self.step_name}_feedback", "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": "JSON parse error, defaulting to rework",
            })
            await _set_state(**_shared_snapshot(shared))
            return self.default_target

        action = decision.get("decision", "rework")
        target = decision.get("target", self.default_target)
        instructions = decision.get("instructions", "")
        current_slot = shared.get("_rejected_slot")

        if self.allowed_targets and target not in self.allowed_targets:
            log.info(f"FeedbackInterpreter: target '{target}' not allowed, "
                     f"falling back to '{self.default_target}'")
            target = self.default_target

        if action == "approve":
            remaining = shared.get("_pending_reject_queue", [])
            if remaining:
                shared["_pending_reject_queue"] = remaining
                shared["_current_step"] = f"{self.step_name}_feedback_approved_next"
                shared["steps"].append({
                    "step": f"{self.step_name}_feedback", "status": "approved",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "output": f"slot {current_slot} approved, {len(remaining)} remaining",
                })
                await _set_state(**_shared_snapshot(shared))
                return "approve"
            else:
                shared["_current_step"] = f"{self.step_name}_feedback_approved"
                shared["steps"].append({
                    "step": f"{self.step_name}_feedback", "status": "approved",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "output": "User approved after feedback",
                })
                await _set_state(**_shared_snapshot(shared))
                return "approve"

        attempts = shared.get("_feedback_attempts", 0) + 1
        shared["_feedback_attempts"] = attempts
        if attempts > 3:
            shared["_error"] = f"FeedbackInterpreter: max feedback attempts exceeded for {self.step_name}"
            shared["_current_step"] = f"{self.step_name}_feedback_error"
            shared["steps"].append({
                "step": f"{self.step_name}_feedback", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": f"max attempts ({attempts}) exceeded",
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            return "error"

        shared[self.feedback_key] = instructions
        shared["user_feedback"] = {
            "target": target,
            "instructions": instructions,
            "raw_text": decision.get("raw_text", ""),
            "slot": current_slot,
        }
        shared["_slots_to_regenerate"] = [current_slot]
        shared["_current_step"] = f"{self.step_name}_feedback_done"
        shared["steps"].append({
            "step": f"{self.step_name}_feedback", "status": "rework",
            "ts": datetime.now(timezone.utc).isoformat(),
            "target": target,
            "instructions": instructions[:500],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return target


def slot_id_type_label(slot_type: str) -> str:
    return {
        "video": "clip vidéo",
        "music": "musique",
        "voiceover": "voix off",
    }.get(slot_type, slot_type)


class ScriptEditFeedbackNode(AsyncNode):
    """Édition DIRECTE du script (bouton ✏️ sur validate_sw_alt).

    Envoie un prompt Telegram et attend le script modifié en réponse :
    - soit un script COMPLET collé ;
    - soit des lignes partielles `Plan N VO: ...` / `Plan N Video: ...`
      (remplace la 1re ligne du type dans le plan concerné).
    Le script est fusionné (apply_partial_edit), retimé (horaires = Σ assets),
    patiemment réparé si la VO dépasse la durée du plan, puis re-gaté pydantic.
    Retourne 'edit' avant de retourner le flow complet vers la validation."""

    def __init__(self, step_name: str = "validate_sw_alt"):
        super().__init__(max_retries=1, wait=5)
        self.step_name = step_name
        self.timeout = TG_VALIDATION_TIMEOUT

    async def prep_async(self, shared):
        shared["_current_step"] = f"{self.step_name}_edit"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        base_vid = f"{shared.get('pipeline_id', 'unknown')}_{self.step_name}_edit"

        if not TG_BOT_TOKEN:
            log.info("[tg] SKIP edit (no token)")
            return json.dumps({"action": "edit", "script": shared.get("script", "")})

        article = shared.get("selected_article", {})
        ctx_head = (
            f"Topic: {shared.get('topic', '?')}\n"
            f"Article: {article.get('title', '?')}"
        )
        full = shared.get("script", "")
        text = (
            f"✏️ Édition du script\n\n{ctx_head}\n\n"
            f"Envoie TES corrections en RÉPONSE à ce message :\n"
            f"- soit le script COMPLET modifié (recommence par `### HOOK`) ;\n"
            f"- soit des lignes partielles, une par plan :\n"
            f"  `Plan 3 VO: nouveau texte de la VO`\n"
            f"  `Plan 3 Video: nouveau prompt vidéo`\n\n"
            f"Script actuel :\n{full[:1200]}"
        )

        event = _register_validation(base_vid)
        msg_id = await send_telegram(text)
        if msg_id:
            _attach_message(base_vid, msg_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            _pending_validations.pop(base_vid, None)
            log.info(f"ScriptEdit {self.step_name}: timeout, no edit applied")
            return json.dumps({"action": "edit", "script": shared.get("script", "")})

        entry = _pending_validations.pop(base_vid, {})
        result = entry.get("result", "")
        if not result.startswith("feedback:"):
            log.info(f"ScriptEdit {self.step_name}: resolved without a script, edit skipped")
            return json.dumps({"action": "edit", "script": shared.get("script", "")})

        edited = result[len("feedback:"):]
        log.info(f"ScriptEdit {self.step_name}: received edit ({len(edited)} chars)")

        try:
            from nodes.scriptwriter.script_timing import (
                apply_partial_edit, rewrite_plan_timestamps,
                pacing_errors, repair_pacing_script,
            )
            from nodes.scriptwriter.pydantic_validation import GeneratedScriptAlt
        except Exception as e:
            log.error(f"ScriptEdit {self.step_name}: import error {e}")

        merged = apply_partial_edit(full, edited)
        merged = rewrite_plan_timestamps(merged)
        if pacing_errors(merged):
            repaired = repair_pacing_script(merged, max_assets=2)
            if repaired is not None:
                merged = repaired
        try:
            GeneratedScriptAlt(script=merged)
        except Exception as e:
            shared["_edit_error"] = str(e)
            shared["script_feedback"] = (
                f"Le script édité ne passe pas la validation : {e}. "
                f"Intègre ces corrections utilisateur dans le nouveau script :\n{merged[:2500]}"
            )
            log.warning(f"ScriptEdit {self.step_name}: edited script invalid ({str(e)[:120]}), "
                        "fallback feedback régénération")

        return json.dumps({"action": "edit", "script": merged})

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error(f"ScriptEdit {self.step_name}: invalid JSON")
            shared["_current_step"] = f"{self.step_name}_edit_done"
            shared["steps"].append({
                "step": f"{self.step_name}_edit", "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": "edit JSON error, script unchanged",
            })
            await _set_state(**_shared_snapshot(shared))
            return "edit"

        shared["script"] = data.get("script", shared.get("script", ""))
        shared["_current_step"] = f"{self.step_name}_edit_done"
        shared["steps"].append({
            "step": f"{self.step_name}_edit", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "output": f"script édité ({len(shared['script'])} chars)",
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "edit"


class ScriptBoostNode(AsyncNode):
    """Bouton ⚡ sur validate_sw_alt : impose la version COMPLÈTE fournie par
    l'utilisateur, en BYPASSANT toutes les validations (fin de l'époque où une
    correction utilisateur était re-gatée pydantic et jetée).

    Le script reçu remplace `script` en l'état — aucun merge partiel, aucun
    retime, aucun filet. L'utilisateur est responsable du format (horaires
    cohérents, structure Plan N/VO:/Video:), l'AssetPlanner en aval le prendra
    tel quel. Retourne 'approve' pour sortir du sous-flow de validation et
    enchaîner vers AssetFinder. Timeout / sans script → 'cancel' (on revient à
    l'attente de validation, jamais d'enchaînement)."""

    def __init__(self, step_name: str = "validate_sw_alt_boost"):
        super().__init__(max_retries=1, wait=5)
        self.step_name = step_name
        self.timeout = TG_VALIDATION_TIMEOUT

    async def prep_async(self, shared):
        shared["_current_step"] = f"{self.step_name}"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        base_vid = f"{shared.get('pipeline_id', 'unknown')}_{self.step_name}"

        if not TG_BOT_TOKEN:
            log.info("[tg] SKIP boost (no token)")
            return json.dumps({"action": "approve", "script": shared.get("script", "")})

        article = shared.get("selected_article", {})
        ctx_head = (
            f"Topic: {shared.get('topic', '?')}\n"
            f"Article: {article.get('title', '?')}"
        )
        full = shared.get("script", "")
        text = (
            f"⚡ Imposer TA version du script\n\n{ctx_head}\n\n"
            f"Envoie ta version COMPLÈTE en RÉPONSE à ce message "
            f"(recommence par `### HOOK`).\n"
            f"Elle remplace le script tel quel, SANS aucune validation : "
            f"veille toi-même aux horaires `Plan N (T-Ts)` (Σ assets, I2V≈4s, "
            f"T2V≈4s), aux lignes `VO:`/`Video:` et à la ligne `Audio:`.\n\n"
            f"Script actuel :\n{full[:800]}"
        )

        event = _register_validation(base_vid)
        msg_id = await send_telegram(text)
        if msg_id:
            _attach_message(base_vid, msg_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            _pending_validations.pop(base_vid, None)
            log.info(f"ScriptBoost {self.step_name}: timeout, no script imposed")
            return json.dumps({"action": "cancel", "script": shared.get("script", "")})

        entry = _pending_validations.pop(base_vid, {})
        result = entry.get("result", "")
        if not result.startswith("feedback:"):
            log.info(f"ScriptBoost {self.step_name}: resolved without a script, cancelled")
            return json.dumps({"action": "cancel", "script": shared.get("script", "")})

        script = result[len("feedback:"):].strip()
        if not script:
            log.info(f"ScriptBoost {self.step_name}: empty script, cancelled")
            return json.dumps({"action": "cancel", "script": shared.get("script", "")})

        log.info(f"ScriptBoost {self.step_name}: imposed script ({len(script)} chars)")
        return json.dumps({"action": "approve", "script": script})

    async def post_async(self, shared, prep, exec):
        try:
            data = json.loads(exec)
        except json.JSONDecodeError:
            log.error(f"ScriptBoost {self.step_name}: invalid JSON")
            shared["_current_step"] = f"{self.step_name}_done"
            shared["steps"].append({
                "step": self.step_name, "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": "boost JSON error, script unchanged",
            })
            await _set_state(**_shared_snapshot(shared))
            return "cancel"

        action = data.get("action", "cancel")
        if action == "approve":
            shared["script"] = data.get("script", shared.get("script", ""))
            shared["_current_step"] = f"{self.step_name}_done"
            shared["steps"].append({
                "step": self.step_name, "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": f"script imposé par bypass ({len(shared['script'])} chars), validations ignorées",
            })
        else:
            shared["_current_step"] = f"{self.step_name}_cancelled"
            shared["steps"].append({
                "step": self.step_name, "status": "cancelled",
                "ts": datetime.now(timezone.utc).isoformat(),
                "output": "aucun script imposé, retour à la validation",
            })
        await _set_state(**_shared_snapshot(shared))
        return action
