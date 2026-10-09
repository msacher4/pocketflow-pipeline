"""VoCoherenceReview — relecture factuelle des VO du script alt.

4 axes de contrôle par `VO:` : cohérence grammaticale (phrase claire pour un
natif), absence d'expressions hallucinées / traductions foireuses, lore accuracy
vs l'article, redondance inter-VO. Plus un filet dur : le personnage central
doit être nommé au moins une fois dans la VO des Plans 1 ou 2. Ne réécrit QUE
les VO fautives ; ne touche jamais aux lignes `Video:`, aux titres `Plan N` ni
aux horaires."""

import json
import logging
import re
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm
from .worker_common import build_common_context

log = logging.getLogger("pocketflow-pipeline")


class VoCoherenceReviewNode(AsyncNode):
    """Contrôle de cohérence VO : chaque `VO:` est évaluée sur 5 axes (sens,
    expression hallucinée/traduction foireuse, noms propres/lore accuracy,
    redondance inter-VO, nommage du perso dans les 2 premiers plans) et
    corrigée, structure du script strictement préservée. Chaque Plan doit
    avoir une entrée d'audit."""

    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "vo_coherence_review"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("vo_coherence_review")
        script = shared.get("script", "")
        self._input_script = script
        ctx = build_common_context(shared)
        ctx += (
            f"\n--- SCRIPT À RELIRE ---\n{script}\n--- FIN SCRIPT ---\n\n"
            f"Évalue le script ci-dessus VO par VO, comme décrit dans ton rôle. "
            f"D'abord l'AUDIT : une entrée par Plan, verdict OK ou CORRIGEE avec "
            f"le défaut accusé. Ensuite corrige UNIQUEMENT les VO fautives : "
            f"même sens, même émotion, même durée possible. Ne touche à aucune "
            f"ligne `Video:`, aucun titre `Plan`, aucun horaire. Retourne "
            f"UNIQUEMENT un JSON valide : {{\"audit\": [{{\"plan\": 1, "
            f"\"verdict\": \"OK|CORRIGEE\", \"defaut\": \"\"}}, ...], "
            f"\"script\": \"le script complet relu et corrigé\"}}"
        )
        llm_resp = await call_llm(LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, max_tokens=8192, timeout=600, temperature=0.2)
        _trace_llm(shared, "vo_coherence_review", "exec", LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, llm_resp)
        decision = _extract_json(llm_resp)

        # Filets en boucle : chaque retry LLM régénère le script COMPLET et peut
        # casser ce qu'un filet précédent avait réparé (le 21/09, le retry "nom
        # du perso" a supprimé le titre "Plan 7 (22-26s)" APRÈS que le filet
        # structure l'avait rétabli). On re-valide donc audit + structure après
        # chaque passe, jusqu'à stabilité (max 3 tours), avec garde-fou dur qui
        # refuse d'émettre un script à structure cassée.
        result = decision
        for _ in range(3):
            before = result.get("script", "") if isinstance(result, dict) else ""
            # Filet 1 : l'audit doit couvrir chaque Plan du script corrigé.
            result = await self._ensure_audit_coverage(shared, soul, ctx, result, llm_resp)
            # Filet 2 : la structure (titres Plan/sections) doit être identique à l'entrée.
            result = await self._ensure_structure_parity(shared, soul, ctx, result)
            # Filet 3 : le personnage doit être nommé dans la VO d'un des 2 premiers plans.
            result = await self._ensure_character_named(shared, soul, ctx, result)
            # Garde-fou : le dernier retry (nom) a pu casser la structure/l'audit.
            result = await self._ensure_structure_parity(shared, soul, ctx, result)
            result = await self._ensure_audit_coverage(shared, soul, ctx, result, llm_resp)
            after = result.get("script", "") if isinstance(result, dict) else ""
            # Routage doux : si le filet nommage a demandé une régénération AltSG,
            # on stoppe les retries LLM du review pour ce script fautif.
            if after == before or shared.get("_route_review"):
                break
        if not shared.get("_route_review"):
            final_script = result.get("script", "") if isinstance(result, dict) else ""
            missing = self._missing_structure_lines(
                self._input_script, final_script or "")
            if missing:
                raise RuntimeError(
                    f"VoCoherenceReview: structure encore altérée après filets: {missing}")
        log.info(f"VoCoherenceReview EXEC -> script length={len(result.get('script', ''))}, "
                 f"audit entries={len(result.get('audit') or [])}")
        return json.dumps(result, ensure_ascii=False)

    def _character_name(self, shared) -> str:
        try:
            char = shared.get("selected_article", {}).get("character", {})
            name = (char.get("name") or "").strip()
        except AttributeError:
            return ""
        return name

    def _franchise_name(self, shared) -> str:
        """Franchise/jeu exigée dans les VO par le validateur pydantic, au même
        titre que le nom du perso (cf. run du 21/09 : 6 régénérations AltSG sur
        'Genshin Impact' non nommé)."""
        try:
            char = shared.get("selected_article", {}).get("character", {})
            franchise = (char.get("franchise") or "").strip()
        except AttributeError:
            return ""
        return franchise

    def _vo_full_text(self, script: str) -> str:
        """Toutes les VO concaténées (même périmètre que le validateur pydantic)."""
        return "\n".join(
            line for line in (script or "").splitlines()
            if re.match(r"^\s*[-*]?\s*VO\s*:", line, re.IGNORECASE)
        ).lower()

    def _plans_missing_character_name(self, script: str, character_name: str) -> list:
        """Retourne [1, 2] si aucun des deux plans (tous les deux) ne nomme le
        perso ; si le nom est présent dans l'un OU l'autre, retourne []."""
        if not character_name or not script:
            return []
        vos_by_plan = []
        plan = None
        for line in script.splitlines():
            m = re.match(r"^Plan (\d+)", line.strip())
            if m:
                plan = int(m.group(1))
                vos_by_plan.append((plan, []))
            elif plan is not None and line.strip().startswith("VO:"):
                vos_by_plan[-1][1].append(line.strip()[3:].strip())
        early = [vos for p, vos in vos_by_plan if p in (1, 2)]
        if not early:
            return []
        named = any(character_name.lower() in v.lower() for vos in early for v in vos)
        return [] if named else [1, 2]

    async def _ensure_character_named(self, shared, soul, ctx, decision):
        if shared.get("_route_review"):
            return decision
        character_name = self._character_name(shared)
        franchise_name = self._franchise_name(shared)
        script = decision.get("script", "")
        missing = self._plans_missing_character_name(script, character_name) if isinstance(script, str) else [1, 2]
        missing_franchise = bool(
            franchise_name and isinstance(script, str)
            and franchise_name.lower() not in self._vo_full_text(script))
        if not missing and not missing_franchise:
            return decision

        log.warning(f"VoCoherenceReview EXEC -> nommage manquant "
                    f"(perso={character_name!r} plans={missing}, "
                    f"franchise={franchise_name!r} absente={missing_franchise})")
        demands = []
        if missing:
            demands.append(
                f"le nom EXACT '{character_name}' dans la VO du Plan 1 "
                f"(Plan 2 seulement si vraiment impossible), en raccourcissant "
                f"le reste de la phrase si nécessaire")
        if missing_franchise:
            demands.append(
                f"le nom EXACT de la franchise '{franchise_name}' dans la VO du "
                f"Plan 1 ou du Plan 2 (idéalement celle du perso, pour que le "
                f"spectateur sache DE QUOI parle la vidéo)")
        retry_ctx = (
            f"{ctx}\n\n--- RAPPEL HORS-RÈGLE ---\n"
            f"Des noms exigés par la validation manquent dans les VO. Insère "
            f"{' et '.join(demands)}, en compressant la phrase pour tenir dans "
            f"la durée du plan (≈ 2 mots/sec), sans trahir le sens ni corriger "
            f"d'autres VOs. Retourne le JSON complet (audit + script corrigé)."
        )
        retry_resp = await call_llm(LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx, max_tokens=8192, timeout=600, temperature=0.2)
        _trace_llm(shared, "vo_coherence_review", "exec_retry_name", LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx, retry_resp)
        retry_decision = _extract_json(retry_resp)
        retry_script = retry_decision.get("script", "") or ""
        missing2 = self._plans_missing_character_name(retry_script, character_name)
        missing_franchise2 = bool(
            franchise_name
            and franchise_name.lower() not in self._vo_full_text(retry_script))
        if missing2 or missing_franchise2:
            log.error(f"VoCoherenceReview EXEC -> nommage toujours manquant "
                      f"(perso plans={missing2}, franchise absente={missing_franchise2})")
            # Routage doux : on ne plante plus le pipeline ; on demande une
            # régénération AltSG avec le défaut en feedback (comme le pydantic).
            shared["_reformat_error"] = (
                f"VoCoherenceReview: nommage toujours manquant après réécriture "
                f"(perso plans={missing2}, franchise absente={missing_franchise2}). "
                f"Exigences de la validation : la VO du Plan 1 (ou 2 au pire) doit "
                f"contenir le nom exact '{character_name}' ; la franchise "
                f"'{franchise_name}' doit apparaître dans une VO (idéalement celle "
                f"du perso)."
            )
            shared["_route_review"] = "reformat_sg"
            return retry_decision
        return retry_decision

    def _missing_audit_plans(self, script: str, audit) -> list:
        plans_in_script = sorted({int(m) for m in re.findall(r"^Plan (\d+)", script, re.M)})
        audited = set()
        if isinstance(audit, list):
            for entry in audit:
                if isinstance(entry, dict) and isinstance(entry.get("plan"), int):
                    audited.add(entry["plan"])
        return [p for p in plans_in_script if p not in audited]

    def _extra_audit_plans(self, script: str, audit) -> list:
        """Plans audités mais ABSENTS du script : symptôme d'un titre Plan perdu
        par un retry (le 21/09 : audit à 7 entrées pour un script à 6 plans)."""
        plans_in_script = {int(m) for m in re.findall(r"^Plan (\d+)", script, re.M)}
        audited = set()
        if isinstance(audit, list):
            for entry in audit:
                if isinstance(entry, dict) and isinstance(entry.get("plan"), int):
                    audited.add(entry["plan"])
        return sorted(p for p in audited if p not in plans_in_script)

    def _missing_structure_lines(self, original_script: str, returned_script: str) -> list:
        grippable = ("Plan ", "### ", "Audio:", "-- Transition --", "SFX:")
        original_lines = [l for l in original_script.splitlines()
                          if l.startswith(grippable) and l not in returned_script]
        return original_lines

    async def _ensure_structure_parity(self, shared, soul, ctx, decision):
        if shared.get("_route_review"):
            return decision
        script = decision.get("script", "")
        missing = self._missing_structure_lines(self._input_script, script) if isinstance(script, str) else []
        if not missing:
            return decision

        log.warning(f"VoCoherenceReview EXEC -> structure altérée, lignes manquantes: {missing}")
        retry_ctx = (
            f"{ctx}\n\n--- RAPPEL HORS-RÈGLE ---\nTon script corrigé a "
            f"supprimé des lignes de structure qu'il faut RETABLIR telles quelles : "
            f"{missing}. Le script doit contenir exactement les mêmes titres "
            f"`Plan N (X-Ys)`, sections `###`, `Audio:`, `SFX:` et transitions "
            f"que l'entrée ; tu modifies le texte des lignes `VO:`, rien d'autre. "
            f"Retourne le JSON complet avec toutes ces lignes rétablies."
        )
        retry_resp = await call_llm(LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx, max_tokens=8192, timeout=600, temperature=0.2)
        _trace_llm(shared, "vo_coherence_review", "exec_retry_struct", LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx, retry_resp)
        retry_decision = _extract_json(retry_resp)
        missing2 = self._missing_structure_lines(self._input_script, retry_decision.get("script", "") or "")
        if missing2:
            log.error(f"VoCoherenceReview EXEC -> structure toujours altérée: {missing2}")
            raise RuntimeError(f"VoCoherenceReview: missing structure lines {missing2}")
        return retry_decision

    async def _ensure_audit_coverage(self, shared, soul, ctx, decision, llm_resp):
        if shared.get("_route_review"):
            return decision
        returned_script = decision.get("script", "")
        audit = decision.get("audit")
        missing = self._missing_audit_plans(returned_script, audit) if isinstance(returned_script, str) else list(range(99))
        extra = self._extra_audit_plans(returned_script, audit) if isinstance(returned_script, str) else []
        if not missing and not extra:
            return decision

        log.warning(f"VoCoherenceReview EXEC -> audit incomplet, Plans manquants: {missing}, "
                    f"entrées orphelines (plan absent du script): {extra}")
        retry_ctx = (
            f"{ctx}\n\n--- RAPPEL HORS-RÈGLE ---\nTon JSON précédent est incomplet : "
            f"il manque les entrées d'audit pour les Plans {missing} (ou l'audit "
            f"n'est pas une liste valable)"
            + (f" ; et les entrées d'audit pour les Plans {extra} ne correspondent "
               f"à AUCUN titre `Plan N` du script — un titre de plan a été perdu, "
               f"rétablis-le tel quel" if extra else "")
            + f". Retourne le JSON complet : audit "
            f"avec UNE entrée par Plan du script (tous les Plans), puis le "
            f"script corrigé. Aucun Plan sans entrée d'audit."
        )
        retry_resp = await call_llm(LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx, max_tokens=8192, timeout=600, temperature=0.2)
        _trace_llm(shared, "vo_coherence_review", "exec_retry", LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx, retry_resp)
        retry_decision = _extract_json(retry_resp)
        retry_missing = self._missing_audit_plans(
            retry_decision.get("script", "") if isinstance(retry_decision.get("script"), str) else "",
            retry_decision.get("audit"),
        )
        retry_extra = self._extra_audit_plans(
            retry_decision.get("script", "") if isinstance(retry_decision.get("script"), str) else "",
            retry_decision.get("audit"),
        )
        if retry_missing or retry_extra:
            log.error(f"VoCoherenceReview EXEC -> audit toujours incomplet: {retry_missing}, orphelines: {retry_extra}")
            raise RuntimeError(f"VoCoherenceReview: audit incomplete, missing plans {retry_missing}, orphan {retry_extra}")
        return retry_decision

    async def post_async(self, shared, prep, exec):
        # Routage doux : un filet (nommage) a demandé une régénération AltSG.
        # On NE COMMIT PAS le script fautif et on renvoie la route, comme le
        # pydantic (reformat_sg). Cap anti-boucle identique (`_sg_regens >= 6`).
        if shared.get("_route_review"):
            route = shared.pop("_route_review", None)
            err = shared.get("_reformat_error", "VoCoherenceReview: nommage non conforme")
            if shared.get("_sg_regens", 0) >= 6:
                shared["_error"] = (
                    f"VoCoherenceReview: nommage toujours manquant après "
                    f"{shared.get('_sg_regens', 0)} régénérations AltSG"
                )
                shared["_current_step"] = "vo_coherence_review_error"
                shared["steps"].append({
                    "step": "vo_coherence_review", "status": "error",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "input": shared.get("script", "")[:500],
                    "output": err[:5000],
                })
                await _set_state(**_shared_snapshot(shared, running=False))
                await _set_traces(shared.get("_traces", {}))
                return "error"
            shared["_current_step"] = "vo_coherence_review_reformat"
            shared["steps"].append({
                "step": "vo_coherence_review", "status": "reformat",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": err[:5000],
            })
            await _set_state(**_shared_snapshot(shared))
            await _set_traces(shared.get("_traces", {}))
            return route

        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("VoCoherenceReview POST -> exec is not valid JSON")
            shared["_current_step"] = "vo_coherence_review_error"
            shared["_error"] = "VoCoherenceReview: exec is not valid JSON"
            shared["steps"].append({
                "step": "vo_coherence_review", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VoCoherenceReview aborted: exec is not valid JSON")

        raw_script = decision.get("script", "")
        if not raw_script.strip():
            log.warning("VoCoherenceReview POST -> script is empty")
            shared["_current_step"] = "vo_coherence_review_error"
            shared["_error"] = "VoCoherenceReview: empty script"
            shared["steps"].append({
                "step": "vo_coherence_review", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VoCoherenceReview aborted: empty script")

        shared["script"] = raw_script
        audit = decision.get("audit") if isinstance(decision.get("audit"), list) else []
        shared["_vo_audit"] = audit
        corrected = sum(1 for e in audit if isinstance(e, dict) and e.get("verdict") == "CORRIGEE")
        log.info(f"VoCoherenceReview POST -> audit: {len(audit)} plans, {corrected} VO(s) corrigée(s)")
        shared["_current_step"] = "vo_coherence_review_done"
        shared["steps"].append({
            "step": "vo_coherence_review", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("script", "")[:500],
            "output": str(exec)[:5000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
