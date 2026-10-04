"""ScriptFixer — node UNIQUE de correction (fusion VoShortener + VideoAssetFixer).

Quand la validation pydantic rejette un script, ce node répare TOUTES les
lignes fautives en une SEULE passe LLM, structure du script strictement
intacte :

1. **VO en sur-budget** : réécrit UNIQUEMENT les lignes `VO:` trop longues
   (même sens, moins de mots) ;
2. **Ligne `Video:` fautive** (erreur préfixée "ASSET(plan N)", bracket/
   statique/headcount JEV) : réécrit la/les SEULE(S) ligne(s) `Video:`
   fautives en prompt LTX-2.5-ready.

Le LLM renvoie uniquement les lignes corrigées indexées par plan (`edits`),
jamais le script complet — aucune dérive de structure. Un filet déterministe
retire mécaniquement les brackets résiduels + raccourcit les VO par paliers.

**Auto-validation (le vrai fix anti-boucle)** : après application, ce node
REVALIDE sa propre sortie avec les MÊMES règles déterministes que le
validateur (budgets VO par plan, brackets/timcodes bannis, jamais de plan
figé). Si le LLM n'a pas convergé au bout de N tentatives, on ESCALADE vers
AltSG (régénération complète) AU LIEU de reboucler sur le fixer — c'est ce
qui tuait les runs (boucle video_asset_fixer → pydantic → 6 régénérations)."""

import json
import logging
import re
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_MODEL
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, _extract_json, _trace_llm, LLMJSONQuoteError
from .script_timing import (
    parse_plans,
    plan_budget_words,
    plan_vo_words,
    static_video_reason,
)

log = logging.getLogger("pocketflow-pipeline")

MAX_WORDS = 20
_EDITS_JSON_EXAMPLE = (
    '{"edits": {"3": {"video": ["nouvelle ligne Video plan 3"], "vo": ["nouvelle VO plan 3"]}}}'
)

# --- Expressions partagées ---
_PLAN_RE = re.compile(r"^\s*[-*]?\s*Plan\s*(\d+)\s*\(", re.IGNORECASE)
_VIDEO_RE = re.compile(r"^(\s*[-*]?\s*Video\s*:\s*)(.+)$", re.IGNORECASE)
_VO_RE = re.compile(r"^(\s*[-*]?\s*VO\s*:\s*)(.+)$", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[\s*\d+\s*[-–]\s*\d+\s*s?\]|\[[^\]]*\]")
_ASSET_ERR_RE = re.compile(r"ASSET\s*\(\s*plan\s*(\d+)\s*\)", re.IGNORECASE)
_VO_LEN_RE = re.compile(r"VO\s+(?:beaucoup\s+)?trop\s+longue", re.IGNORECASE)


def _count_words(text: str) -> int:
    """Comptage OFFICIEL du pipeline : contractions ('she's', 'you'll') = 2 mots."""
    return len([w for w in (text or "").replace("'", " ").split() if w.strip()])


def _faulty_plans_video(error: str) -> list[int]:
    if not error:
        return []
    return sorted({int(m) for m in _ASSET_ERR_RE.findall(error)})


def _faulty_plans_vo(error: str) -> list[int]:
    """Plans avec VO en sur-budget, détectés par le comptage déterministe
    (pas seulement par le message d'erreur)."""
    return sorted({p["num"] for p in parse_plans(shared["script"]) if False}) if False else []


def _vo_lines_per_plan(script: str) -> dict[int, list[tuple[int, str]]]:
    """Lignes VO par plan : {plan_num: [(line_index, 'texte'), ...]}."""
    out: dict[int, list[tuple[int, str]]] = {}
    cur: int | None = None
    for i, raw in enumerate((script or "").splitlines()):
        m = _PLAN_RE.match(raw)
        if m:
            cur = int(m.group(1))
            continue
        mv = _VO_RE.match(raw)
        if cur is not None and mv:
            out.setdefault(cur, []).append((i, mv.group(2)))
    return out


def _video_lines_per_plan(script: str) -> dict[int, list[tuple[int, str]]]:
    """Lignes Video par plan : {plan_num: [(line_index, 'texte'), ...]}."""
    out: dict[int, list[tuple[int, str]]] = {}
    cur: int | None = None
    for i, raw in enumerate((script or "").splitlines()):
        m = _PLAN_RE.match(raw)
        if m:
            cur = int(m.group(1))
            continue
        mv = _VIDEO_RE.match(raw)
        if cur is not None and mv:
            out.setdefault(cur, []).append((i, mv.group(2)))
    return out


def _strip_brackets_deterministic(script: str, only_plans: set[int] | None = None) -> tuple[str, bool]:
    """Filet sans LLM : retire mécaniquement brackets/timcodes des lignes Video
    (dans les plans visés si `only_plans`). Retourne (script, changed)."""
    lines = (script or "").splitlines()
    cur: int | None = None
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
    return ("\n".join(lines) if changed else script), changed


class ScriptFixerNode(AsyncNode):
    """Répare VO sur-budget ET/OU lignes Video: fautives en une seule passe,
    structure intacte. S'auto-valide AVANT de rendre la main (vrai fix de la
    boucle reformat) et escalade vers AltSG si le filet ne converge pas."""

    def __init__(self):
        super().__init__(max_retries=2, wait=30)
        self.step_name = "script_fixer"

    async def prep_async(self, shared):
        shared["_current_step"] = "script_fixer"
        await _set_state(**_shared_snapshot(shared))
        return shared

    def _self_validate(self, script: str, shared) -> list[str]:
        """Validation déterministe de la SORTIE : retourne la liste des erreurs
        résiduelles, VIDE si le script est propre.

        La règle « plan FIGÉ » est déléguée à `script_timing.static_video_reason`
        — la MÊME fonction que le validateur pydantic. Les deux copies
        locales avaient divergé (regex `frozen` en dur ici, liste différente
        là), ce qui rendait l'auto-validation incapable de voir un défaut que
        le validateur, lui, rejetait."""
        errors: list[str] = []
        for p in parse_plans(script):
            budget = plan_budget_words(p)
            total = plan_vo_words(p)
            if total > budget:
                errors.append(f"VO plan {p['num']} : {total} mots > budget {budget}")
        cur: int | None = None
        for raw in (script or "").splitlines():
            m = _PLAN_RE.match(raw)
            if m:
                cur = int(m.group(1))
                continue
            if not _VIDEO_RE.match(raw):
                continue
            low = raw.lower()
            prefix = f"ASSET(plan {cur})" if cur is not None else "ASSET"
            if "[" in raw:
                errors.append(f"{prefix}: bracket résiduel: {raw.strip()[:80]}")
                continue
            if low.startswith("a video of") or low.startswith("cinematic shot of"):
                errors.append(f"{prefix}: ouverture interdite (filler): {raw.strip()[:80]}")
                continue
            n_ends = len(re.findall(r"[.!?]+", raw))
            n_words = len(raw.split())
            if n_ends < 3 and n_words < 25:
                errors.append(f"{prefix}: Video trop courte ({n_words} mots, {n_ends} phrase(s))")
                continue
            reason = static_video_reason(raw)
            if reason:
                errors.append(f"{prefix}: plan FIGÉ interdit: {reason} — {raw.strip()[:80]}")
        return errors

    async def exec_async(self, shared):
        soul = load_soul("script_fixer")
        script = shared.get("script", "")
        error = shared.get("_reformat_error", "")
        self._input_script = script
        self._faulty_video = _faulty_plans_video(error)
        self._faulty_vo = _faulty_plans_vo(error)

        # Filet déterministe d'abord : brackets retirés où c'est le problème.
        fixed, _ = _strip_brackets_deterministic(script, set(self._faulty_video) if self._faulty_video else None)

        # Détection VO sur-budget (déterministe, pas seulement le message).
        if not self._faulty_vo:
            self._faulty_vo = [
                p["num"] for p in parse_plans(fixed)
                if plan_vo_words(p) > plan_budget_words(p)
            ]

        targets = sorted(set(self._faulty_video) | set(self._faulty_vo))
        if not targets:
            # Aucun plan fautif identifiable → filet brackets sur tout.
            fixed, _ = _strip_brackets_deterministic(fixed, None)
            return json.dumps({"script": fixed}, ensure_ascii=False)

        per_plan_video = _video_lines_per_plan(fixed)
        per_plan_vo = _vo_lines_per_plan(fixed)

        ctx_lines: dict[int, dict[str, str]] = {}
        for pn in targets:
            entry: dict[str, str] = {}
            if pn in per_plan_vo:
                entry["vo"] = " ; ".join(t for _, t in per_plan_vo[pn])
            if pn in per_plan_video:
                entry["video"] = " ; ".join(t for _, t in per_plan_video[pn])
            if entry:
                ctx_lines[pn] = entry

        ctx = (
            f"\n--- SCRIPT À CORRIGER ---\n{fixed}\n--- FIN SCRIPT ---\n\n"
            f"La validation a rejeté le script.\n"
            f"ERREUR EXACTE DU VALIDATEUR :\n{error}\n\n"
            f"Plans fautifs à corriger : {sorted(targets)} "
            f"(VO en sur-budget et/ou lignes Video: fautives — brackets, plan "
            f"figé/statique, headcount JEV).\n"
            f"RÉÉCRIS UNIQUEMENT les lignes fautives de ces plans :\n"
            f"- VO: → même sens, moins de mots (budget ≈ 2 mots/sec × durée des "
            f"assets ; contractions comptent DOUBLE ; max 2 vidéos/plan).\n"
            f"- Video: → UN paragraphe fluide au présent, 4-8 phrases, qui ouvre "
            f"par l'action puis couvre décor précis, personnage, mouvement caméra, "
            f"son, lumière. ZÉRO bracket '[0-1s]', zéro texte à l'écran, jamais de "
            f"plan figé. Si une seule personne visible → 'alone' dès la première "
            f"clause. Deux personnes → nomme-les par type. Foule → 'crowd blurred "
            f"in the background'.\n"
            f"Lignes à corriger :\n{json.dumps(ctx_lines, ensure_ascii=False)}\n\n"
            f"Renvoie UNIQUEMENT les lignes corrigées en JSON au format: {_EDITS_JSON_EXAMPLE}"
        )

        llm_resp = await call_llm(
            LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx,
            max_tokens=8192, timeout=600, temperature=0.2,
        )
        _trace_llm(shared, "script_fixer", "exec", LLM_SCRIPTWRITER_ALT_MODEL, soul, ctx, llm_resp)
        try:
            decision = _extract_json(llm_resp)
        except LLMJSONQuoteError as e:
            log.warning(f"ScriptFixer EXEC -> JSON quote error, retrying clean re-emit: {e}")
            retry_ctx = (
                f"{ctx}\n\n--- RÉPONSE PRÉCÉDENTE INVALIDE ---\nTon JSON est invalide : "
                f"tu as écrit des guillemets doubles à l'intérieur des lignes Video/VO "
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
                _trace_llm(shared, "script_fixer", "exec_retry_quotes", LLM_SCRIPTWRITER_ALT_MODEL, soul, retry_ctx, retry_resp)
                decision = _extract_json(retry_resp)
            except Exception as e2:
                log.warning(f"ScriptFixer EXEC -> re-emit still invalid ({e2}), filet déterministe seul")
                decision = None

        edits = decision.get("edits") if isinstance(decision, dict) else None
        fixed_final = self._apply_edits(fixed, edits) if edits else fixed
        # Filet : brackets résiduels éventuels dans les lignes réécrites.
        fixed_final, _ = _strip_brackets_deterministic(fixed_final, set(targets))
        log.info(f"ScriptFixer EXEC -> plans={sorted(targets)}, length={len(fixed_final)}")
        self._raw_script = fixed_final
        self._edits = edits
        return json.dumps({"script": fixed_final}, ensure_ascii=False)

    def _apply_edits(self, script: str, edits) -> str:
        """Remplace les lignes Video:/VO: des plans corrigés par les nouvelles
        fournies par le LLM. Une ligne par nouveau texte."""
        if not isinstance(edits, dict) or not edits:
            return script

        lines = (script or "").splitlines()
        per_plan_video = _video_lines_per_plan(script)
        per_plan_vo = _vo_lines_per_plan(script)

        new_text: dict[int, dict[str, list[str]]] = {}
        for pn_raw, entry in edits.items():
            try:
                pn = int(pn_raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                entry = {"video": [entry] if not isinstance(entry, list) else entry}
            per: dict[str, list[str]] = {}
            for k in ("video", "vo"):
                raw_v = entry.get(k)
                if isinstance(raw_v, str):
                    raw_v = [raw_v]
                kept = [str(t).strip() for t in (raw_v or []) if str(t).strip()]
                if kept:
                    per[k] = kept
            if per:
                new_text[pn] = per

        replace: dict[int, str] = {}
        for pn, per in new_text.items():
            for kind in ("video", "vo"):
                targets = per_plan_video.get(pn, []) if kind == "video" else per_plan_vo.get(pn, [])
                regexp = _VIDEO_RE if kind == "video" else _VO_RE
                for (idx, _), t in zip(targets, per.get(kind, [])):
                    m = regexp.match(lines[idx])
                    if m:
                        replace[idx] = m.group(1) + t

        if not replace:
            return script
        return "\n".join(replace.get(i, l) for i, l in enumerate(lines))

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("ScriptFixer POST -> exec is not valid JSON")
            shared["_current_step"] = "script_fixer_error"
            shared["_error"] = "ScriptFixer: exec is not valid JSON"
            shared["steps"].append({
                "step": "script_fixer", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ScriptFixer aborted: exec is not valid JSON")

        raw_script = decision.get("script", "")
        if not raw_script.strip():
            log.warning("ScriptFixer POST -> script is empty")
            shared["_current_step"] = "script_fixer_empty"
            shared["_error"] = "ScriptFixer: empty script"
            shared["steps"].append({
                "step": "script_fixer", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("script", "")[:500],
                "output": str(exec)[:5000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("ScriptFixer aborted: empty script")

        # AUTO-VALIDATION (le vrai fix) : on re-évalue notre propre sortie.
        residual = self._self_validate(raw_script, shared)
        self._residual_errors = residual
        if residual:
            # Garde-fou d'escalade : 2 passes max, puis AltSG. Compteur DÉDIÉ
            # au fixer (pas partagé avec pydantic) pour un vrai budget de retry.
            shared["_reformat_error"] = "; ".join(residual)
            shared["_fixer_attempts"] = shared.get("_fixer_attempts", 0) + 1
            if shared["_fixer_attempts"] >= 2:
                attempts = shared["_fixer_attempts"]
                shared["_fixer_attempts"] = 0
                log.warning(
                    f"ScriptFixer -> non-convergence après {attempts} "
                    f"tentatives, escalade AltSG: {'; '.join(residual)[:200]}")
                shared["steps"].append({
                    "step": "script_fixer", "status": "escalate",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "input": self._input_script[:500],
                    "output": "; ".join(residual)[:500],
                })
                await _set_state(**_shared_snapshot(shared))
                await _set_traces(shared.get("_traces", {}))
                return "reformat_sg"
            log.info(f"ScriptFixer -> auto-validation KO (tentative {shared['_fixer_attempts']}), refix")
            # On conserve la 1ʳᵉ passe : la 2ᵉ repart du script partiellement
            # corrigé, pas de l'original (sinon elle répète les mêmes edits).
            shared["script"] = raw_script
            shared["steps"].append({
                "step": "script_fixer", "status": "refix",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": self._input_script[:500],
                "output": "; ".join(residual)[:500],
            })
            await _set_state(**_shared_snapshot(shared))
            await _set_traces(shared.get("_traces", {}))
            return "refix"

        shared["_fixer_attempts"] = 0
        shared["script"] = raw_script
        shared["_reformat_error"] = ""
        shared["_current_step"] = "script_fixer_done"
        shared["steps"].append({
            "step": "script_fixer", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": self._input_script[:200],
            "output": raw_script[:200],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
