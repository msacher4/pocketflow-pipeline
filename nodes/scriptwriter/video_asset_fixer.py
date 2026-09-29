"""VideoAssetFixer — réécrit UNIQUEMENT les lignes `Video:` fautives (brackets/
statique/headcount JEV), alternative à la régénération complète (AltSG).

Quand la validation pydantic rejette un script pour UNE ligne Video précise
(erreur préfixée "ASSET(plan N)"), ce node réécrit la/les SEULE(S) ligne(s)
`Video:` fautives en prompt LTX-2.5-ready — mêmes règles que le soul SW
(paragraphe 4-8 phrases au présent, action + décor physique + caméra + son +
lumière, `alone` si seule, zéro bracket/timcode, zéro texte, jamais figé) —
sans toucher à VO/Audio/horaires/structure.

Pattern strictement identique à VoShortener : le LLM renvoie uniquement les
lignes corrigées indexées par plan (`edits`), jamais le script complet. Un filet
déterministe retire mécaniquement les brackets résiduels ; si le headcount JEV
rejette encore, on escalade vers AltSG (régénération complète)."""

import json
import logging
import re
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, LLMJSONQuoteError

log = logging.getLogger("pocketflow-pipeline")

_VIDEO_RE = re.compile(r"^(\s*[-*]?\s*Video\s*:\s*)(.+)$", re.IGNORECASE)
_PLAN_RE = re.compile(r"^\s*[-*]?\s*Plan\s*(\d+)\s*\(", re.IGNORECASE)
# Erreurs d'asset ciblées (voir pydantic_validation) : ASSET(plan N): message
_ASSET_ERR_RE = re.compile(r"ASSET\s*\(\s*plan\s*(\d+)\s*\)", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[\s*\d+\s*[-–]\s*\d+\s*s?\]|\[[^\]]*\]")

_EDITS_JSON_EXAMPLE = '{"edits": {"3": ["nouvelle ligne Video plan 3"], "5": ["nouvelle ligne Video plan 5"]}}'


def _faulty_plans(error: str) -> list[int]:
    """Plans fautifs extraits de l'erreur pydantic ("ASSET(plan 3): ...")."""
    if not error:
        return []
    return sorted({int(m) for m in _ASSET_ERR_RE.findall(error)})


def _video_lines_per_plan(script: str) -> dict[int, list[int]]:
    """Lignes Video: par plan : {plan_num: [line_index, ...]}."""
    out: dict[int, list[int]] = {}
    cur = None
    for i, raw in enumerate((script or "").splitlines()):
        m = _PLAN_RE.match(raw)
        if m:
            cur = int(m.group(1))
            continue
        if cur is not None and _VIDEO_RE.match(raw):
            out.setdefault(cur, []).append(i)
        elif cur is None and _VIDEO_RE.match(raw):
            out.setdefault(0, []).append(i)
    return out


def _strip_brackets_deterministic(script: str, only_plans: set[int] | None = None) -> str:
    """Filet sans LLM : retire mécaniquement les brackets/timcodes des lignes
    Video: (uniquement dans les plans visés si `only_plans`). Zéro réécriture."""
    lines = (script or "").splitlines()
    cur = None
    changed = False
    for i, raw in enumerate(lines):
        m = _PLAN_RE.match(raw)
        if m:
            cur = int(m.group(1))
            continue
        mv = _VIDEO_RE.match(raw)
        if not mv:
            continue
        if only_plans is not None and cur not in only_plans:
            continue
        cleaned = _BRACKET_RE.sub("", mv.group(2))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned != mv.group(2):
            lines[i] = mv.group(1) + cleaned
            changed = True
    return "\n".join(lines) if changed else script


class VideoAssetFixerNode(AsyncNode):
    """Réécrit UNIQUEMENT les lignes Video: fautives d'un script, structure
    (VO/Audio/sections/horaires) strictement intacte. S'applique AUSSI aux I2V :
    une Video: I2V flaggée (headcount JEV, bracket, figée) est réécrite ici, et
    rewrite_i2v_prompt affinera encore le prompt final depuis le portrait réel."""

    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "video_asset_fixer"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("video_asset_fixer")
        script = shared.get("script", "")
        error = shared.get("_reformat_error", "")
        self._input_script = script
        self._faulty = _faulty_plans(error)

        if not self._faulty:
            log.warning("VideoAssetFixer -> erreur sans 'ASSET(plan N):', filet brackets sur tout")
            fixed = _strip_brackets_deterministic(script, None)
            return json.dumps({"script": fixed}, ensure_ascii=False)

        per_plan = _video_lines_per_plan(script)
        targets = {p: per_plan[p] for p in self._faulty if p in per_plan}
        if not targets:
            log.warning(f"VideoAssetFixer -> plans fautifs {self._faulty} sans ligne Video, aborted")
            raise RuntimeError(f"VideoAssetFixer: plans fautifs {self._faulty} sans ligne Video: ")

        # Filet déterministe d'abord : brackets retirés là où c'est le problème.
        fixed = _strip_brackets_deterministic(script, set(self._faulty))
        ctx_lines = {}
        for raw in fixed.splitlines():
            m = _PLAN_RE.match(raw)
            if m:
                cur = int(m.group(1))
                continue
            mv = _VIDEO_RE.match(raw)
            if mv and cur in targets:
                ctx_lines.setdefault(cur, []).append(mv.group(2))

        ctx = (
            f"\n--- SCRIPT À CORRIGER ---\n{fixed}\n--- FIN SCRIPT ---\n\n"
            f"La validation a rejeté le script pour des lignes Video: fautives "
            f"(plans {sorted(targets)}). RÉÉCRIS UNIQUEMENT les lignes Video: de ces "
            f"plans, en GUARDANT la structure, les horaires, les VO et le sens des "
            f"autres lignes STRICTEMENT INTACTES.\n"
            f"Format LTX-2.5 requis pour chaque ligne Video: : UN paragraphe fluide au "
            f"présent, 4-8 phrases, qui ouvre par l'action puis couvre décor physique "
            f"précis, personnage, mouvement caméra, son diégétique, lumière. ZÉRO "
            f"bracket/timcode '[0-1s]', zéro texte à l'écran, jamais de plan figé. "
            f"Si une seule personne visible → écris 'alone' dès la première clause. "
            f"Si deux personnes → nomme-les par type. En cas de foule → "
            f"'crowd blurred in the background'.\n"
            f"Lignes à corriger :\n{json.dumps(ctx_lines, ensure_ascii=False)}\n\n"
f"Renvoie UNIQUEMENT les lignes corrigées en JSON au format: {_EDITS_JSON_EXAMPLE}"
        )

        llm_resp = await call_llm(
            LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx,
            max_tokens=8192, timeout=600, temperature=0.2,
        )
        _trace_llm(shared, "video_asset_fixer", "exec", LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, llm_resp)
        try:
            decision = _extract_json(llm_resp)
        except LLMJSONQuoteError as e:
            log.warning(f"VideoAssetFixer EXEC -> JSON quote error, retrying clean re-emit: {e}")
            retry_ctx = (
                f"{ctx}\n\n--- RÉPONSE PRÉCÉDENTE INVALIDE ---\nTon JSON est invalide : "
                f"tu as écrit des guillemets doubles à l'intérieur des lignes Video: "
                f"(interdit). Réémet EXACTEMENT les mêmes edits, sans rien changer au "
                f"contenu, mais en remplaçant TOUS les guillemets doubles internes par "
                f"des apostrophes simples '. Retourne UNIQUEMENT le JSON corrigé au "
                f"format: {_EDITS_JSON_EXAMPLE}"
            )
            try:
                retry_resp = await call_llm(
                    LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx,
                    max_tokens=8192, timeout=600, temperature=0.2,
                )
                _trace_llm(shared, "video_asset_fixer", "exec_retry_quotes",
                           LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx, retry_resp)
                decision = _extract_json(retry_resp)
                log.info("VideoAssetFixer EXEC -> clean re-emit parsed OK")
            except Exception as e2:
                log.warning(f"VideoAssetFixer EXEC -> re-emit still invalid ({e2}), filet déterministe seul")
                decision = None

        edits = decision.get("edits") if isinstance(decision, dict) else None
        fixed_final = self._apply_edits(fixed, edits) if edits else fixed
        # Garde-fou : brackets résiduels éventuels dans les lignes réécrites.
        fixed_final = _strip_brackets_deterministic(fixed_final, set(self._faulty))
        log.info(f"VideoAssetFixer EXEC -> plans={sorted(self._faulty)}, length={len(fixed_final)}")
        return json.dumps({"script": fixed_final}, ensure_ascii=False)

    def _apply_edits(self, script: str, edits) -> str:
        """Remplace les lignes Video: des plans corrigés par les nouvelles fournies
        par le LLM. Une ligne par nouveau texte ; toute nouvelle Video dupliquée
        dans un plan est ignorée (au plus 2 par plan)."""
        if not isinstance(edits, dict) or not edits:
            return script

        lines = (script or "").splitlines()
        per_plan_video_lines = _video_lines_per_plan(script)

        new_text: dict[int, list[str]] = {}
        for pn_raw, texts in edits.items():
            try:
                pn = int(pn_raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(texts, list):
                texts = [texts]
            kept = [str(t).strip() for t in texts if str(t).strip()]
            if kept:
                new_text[pn] = kept

        replace: dict[int, str] = {}
        for pn, texts in new_text.items():
            targets = per_plan_video_lines.get(pn, [])
            for idx, t in zip(targets, texts):
                m = _VIDEO_RE.match(lines[idx])
                if m:
                    replace[idx] = m.group(1) + t

        if not replace:
            return script
        return "\n".join(replace.get(i, l) for i, l in enumerate(lines))

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("VideoAssetFixer POST -> exec is not valid JSON")
            shared["_current_step"] = "video_asset_fixer_error"
            shared["_error"] = "VideoAssetFixer: exec is not valid JSON"
            shared["steps"].append({
                "step": "video_asset_fixer", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VideoAssetFixer aborted: exec is not valid JSON")

        raw_script = decision.get("script", "")
        if not raw_script.strip():
            log.warning("VideoAssetFixer POST -> script is empty")
            shared["_current_step"] = "video_asset_fixer_error"
            shared["_error"] = "VideoAssetFixer: empty script"
            shared["steps"].append({
                "step": "video_asset_fixer", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("VideoAssetFixer aborted: empty script")

        shared["script"] = raw_script
        shared["_reformat_error"] = ""
        shared["_current_step"] = "video_asset_fixer_done"
        shared["steps"].append({
            "step": "video_asset_fixer", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": self._input_script[:200],
            "output": raw_script[:200],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"