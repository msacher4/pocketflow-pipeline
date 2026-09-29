import json
import logging
import re
from datetime import datetime, timezone

import httpx

from config import LLM_URL, SOULS_DIR, KNOWLEDGE_DIR

log = logging.getLogger("pocketflow-pipeline")

def _init_trace(shared, step: str) -> dict:
    traces = shared.setdefault("_traces", {})
    t = traces.setdefault(step, {"manager": {}, "agent": {}})
    return t

def _trace_llm(shared, step: str, phase: str, model: str, system: str, user_msg: str, response: str):
    t = _init_trace(shared, step)
    t["manager"][phase] = {
        "model": model,
        "system_preview": system[:300],
        "user_msg_preview": user_msg[:500],
        "response_preview": response[:2000],
        "ts": datetime.now(timezone.utc).isoformat(),
    }

def _trace_agent(shared, step: str, action: str, prompt: str = "", response: str = "", fallback_content: str = ""):
    t = _init_trace(shared, step)
    t["agent"] = {
        "action": action,
        "prompt_preview": prompt[:500],
        "response_preview": response[:2000],
        "fallback_content_preview": fallback_content[:500],
        "ts": datetime.now(timezone.utc).isoformat(),
    }

class LLMJSONQuoteError(ValueError):
    """Levée quand un JSON LLM est invalide à cause de guillemets doubles
    non échappés dans une valeur string (ex: "Hook" au milieu du texte)."""


def _scan_string_quotes(text: str) -> list[int]:
    """Repère les `"` qui ferment une string JSON mais sont suivis d'un
    caractère non-délimiteur (donc en réalité un guillemet interne non échappé).

    Retourne les index de ces guillemets fautifs.
    """
    bad = []
    in_string = False
    escaped = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                nxt = text[i + 1] if i + 1 < n else ""
                if nxt and nxt not in ',}]:\n\r\t ':
                    bad.append(i)
            i += 1
        else:
            if ch == '"':
                in_string = True
            i += 1
    return bad


_JSON_ESCAPABLE = {'"', '\\', '/', 'b', 'f', 'n', 'r', 't', 'u'}


def _json_escape_string(raw: str) -> str:
    """Réchappe une valeur string brute en préservant les séquences déjà valides.

    Seuls les caractères réellement fautifs sont échappés : `"` nus, antislashs
    non suivis d'un échappement JSON valide, et retours à la ligne réels. Les
    séquences déjà correctes (`\\n`, `\\"`, `\\\\`, `\\uXXXX`...) restent intactes.
    """
    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == '\\':
            if i + 1 < n and raw[i + 1] in _JSON_ESCAPABLE:
                out.append('\\')
                out.append(raw[i + 1])
                i += 2
            else:
                out.append('\\\\')
                i += 1
        elif ch == '"':
            out.append('\\"')
            i += 1
        elif ch == '\n':
            out.append('\\n')
            i += 1
        elif ch == '\r':
            out.append('\\r')
            i += 1
        elif ch == '\t':
            out.append('\\t')
            i += 1
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def _find_object_end(text: str, start: int) -> int:
    """Repère la `}` fermant l'objet top-level en ignorant le contenu des strings."""
    n = len(text)
    depth = 0
    in_str = False
    escaped = False
    i = start
    while i < n:
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _find_last_unescaped_quote(text: str, lo: int, hi: int) -> int:
    """Dernier `"` non-échappé dans text[lo:hi] (la vraie fermeture de string)."""
    i = hi - 1
    while i >= lo:
        if text[i] == '"':
            backslashes = 0
            j = i - 1
            while j >= lo and text[j] == '\\':
                backslashes += 1
                j -= 1
            if backslashes % 2 == 0:
                return i
            i -= backslashes
        i -= 1
    return -1


def _repair_quotes_aggressive(text: str) -> str:
    """Repair unescaped double quotes inside JSON string values.

    Strategy: tokenise le top-level {..} hors string, localise la vraie
    fermeture de la valeur (dernier `"` non-échappé avant la `}` de fin), puis
    ré-échappe proprement la valeur sans corrompre les séquences déjà valides.
    """
    import re
    m = re.search(r'\s*\{', text)
    if not m:
        return text
    start = m.start()
    try:
        result, _ = json.JSONDecoder().raw_decode(text[start:])
        if isinstance(result, dict):
            return text
    except json.JSONDecodeError:
        pass
    key_m = re.match(r'\s*\{\s*"([^"]+)"\s*:\s*"', text[start:])
    if not key_m:
        return text
    key = key_m.group(1)
    val_start = start + key_m.end()
    obj_end = _find_object_end(text, start)
    if obj_end < 0:
        return text
    val_end = _find_last_unescaped_quote(text, val_start, obj_end)
    if val_end <= val_start:
        return text
    raw_val = text[val_start:val_end]
    fixed_val = _json_escape_string(raw_val)
    repaired = text[:start] + '{' + '"' + key + '"' + ': ' + '"' + fixed_val + '"' + '}'
    return repaired


def _repair_quotes(text: str) -> str:
    """Remplace par des apostrophes les guillemets doubles internes non échappés.

    Fonctionne par paire : quand un guillemet ferme une string mais est suivi
    d'un caractère non-structurel (ex: ``"reason": "Le gars a dit "cool" hier"``),
    on le traite comme un guillemet de contenu, et le guillemet de fin de phrase
    correspondant est converti en apostrophe aussi — sans fermer prématurément
    la string JSON englobante.
    """
    structural = {",", "}", "]", ":"}
    out = list(text)
    in_string = False
    escaped = False
    quoted_phrase = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                rest = text[i + 1:] if i + 1 < n else ""
                first = rest.lstrip()[:1]
                if quoted_phrase:
                    out[i] = "'"
                    quoted_phrase = False
                elif first and first not in structural:
                    out[i] = "'"
                    quoted_phrase = True
                else:
                    in_string = False
            i += 1
        else:
            if ch == '"':
                in_string = True
            i += 1
    return "".join(out)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.find("```", start)
        text = (text[start:end] if end >= 0 else text[start:]).strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.find("```", start)
        text = (text[start:end] if end >= 0 else text[start:]).strip()
    brace = text.find("{")
    if brace >= 0:
        text = text[brace:]
    try:
        result, _ = json.JSONDecoder().raw_decode(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    for attempt in [
        text,
        text.replace("'", '"'),
        text.replace("\r", "").replace("\n", " "),
        text.replace("\r", "").replace("\n", " ").replace("'", '"'),
    ]:
        attempt = attempt.strip()
        if not attempt:
            continue
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    import re
    fixed = text
    fixed = re.sub(r'="([^"]*)"', r"='\1'", fixed)
    fixed = re.sub(r'(?<=[\w,])"(?=\s)', "'", fixed)
    for attempt in [fixed, fixed.replace("'", '"'), fixed.replace("\r", "").replace("\n", " ")]:
        attempt = attempt.strip()
        if not attempt:
            continue
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    py_fixed = fixed.replace('"', "'")
    import ast
    try:
        result = ast.literal_eval(py_fixed)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    if '"' in text:
        repaired = _repair_quotes(text)
        if repaired != text:
            try:
                return _extract_json(repaired)
            except (LLMJSONQuoteError, ValueError):
                pass
    aggressive = _repair_quotes_aggressive(text)
    if aggressive != text:
        try:
            return _extract_json(aggressive)
        except (LLMJSONQuoteError, ValueError):
            pass
    # Dernier recours : { "script": "..." } — on traite tout le contenu entre
    # les délimiteurs comme la valeur brute (guillemets internes non échappés
    # inclus, ils font partie du script vidéo).
    import re as _r
    m = _r.search(r'\s*\{\s*"script"\s*:\s*"', text)
    if m:
        start = m.end()
        end = text.rfind('"}')
        if end > start:
            raw_val = text[start:end]
            # Le modèle n'échappe pas nécessairement \n / \\ : normalise en dur.
            clean = raw_val.replace("\r\n", "\n").replace("\r", "\n")
            clean = clean.replace("\\n", "\n")
            return {"script": clean}
    raise LLMJSONQuoteError(
            "JSON LLM invalide : guillemets doubles non échappés dans une valeur string. "
            f"Contexte: {text[:500]}"
        )

def load_soul(name: str) -> str:
    path = SOULS_DIR / f"{name}.md"
    if path.is_file():
        return path.read_text().strip()
    log.warning(f"Soul file not found: {path}")
    return ""

def load_knowledge(name: str) -> str:
    path = KNOWLEDGE_DIR / f"{name}.md"
    if path.is_file():
        return path.read_text().strip()
    log.warning(f"Knowledge file not found: {path}")
    return ""

def extract_knowledge_section(knowledge: str, section_name: str) -> str:
    """Extract a single '## N. The \"Name\"' section from a knowledge file."""
    if not section_name.strip():
        return ""
    sections = re.split(r'\n## ', '\n' + knowledge)
    target = section_name.strip().casefold()
    for s in sections[1:]:
        header = s.splitlines()[0].casefold()
        if target in header or f'the "{section_name.strip().casefold()}"' in header:
            return ('## ' + s).strip()
    log.warning(f"extract_knowledge_section: section {section_name!r} not found")
    return ""

async def call_llm(model: str, system: str, user_msg: str, timeout: int = 180, max_tokens: int = 4096, reasoning_max_tokens: int = 0, url: str = None, api_key: str = None, session: str = None, temperature: float = None) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if reasoning_max_tokens > 0:
        body["reasoning_max_tokens"] = reasoning_max_tokens
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    if session:
        headers = (headers or {}) | {"x-opencode-session": session}
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url or LLM_URL, json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content")
        if not content:
            raise RuntimeError(
                f"call_llm: réponse vide (content null) sur {model}. "
                f"finish_reason={data['choices'][0].get('finish_reason')}, "
                f"reasoning_tokens présents — budget max_tokens trop faible ou modèle à raisonnement."
            )
        return content.strip()
