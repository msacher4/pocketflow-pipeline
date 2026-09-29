"""Fetch d'article via browser-harness (instance headless Ungoogled Chromium).

Fallback de FetchArticleContentNode : ouvre la page directement dans un vrai
navigateur piloté par CDP, et en dernier recours résout un widget Turnstile /
reCAPTCHA via CapBypass (https://api.capbypass.pro).
"""
import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.request

from config import LLM_URL, LLM_MODEL
from config_actufinder import (
    BH_BIN,
    BH_CDP,
    BH_PROFILE,
    BH_TIMEOUT,
    BH_AGENT_TIMEOUT,
    BH_AGENT_STEPS,
    BH_AGENT_MODEL,
    CAPBYPASS_API_KEY,
    CAPBYPASS_BASE_URL,
)

log = logging.getLogger("pocketflow-pipeline")

# Nom d'isolation du daemon browser-harness pour ce pipeline (ne pas mélanger
# avec le daemon "default" attaché au Chrome graphique de la session).
BU_NAME = "pf_browser"

_CDP_HTTP = f"http://{BH_CDP}/json/version"
_FLATPAK = "/usr/bin/flatpak"
_ARTICLE_CONTENT_MAX = 4000


def _cdp_alive() -> bool:
    try:
        with urllib.request.urlopen(_CDP_HTTP, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_headless_browser(timeout: float = 30.0) -> None:
    """Lance l'instance headless dédiée si le CDP 9222 ne répond pas."""
    if _cdp_alive():
        return
    cmd = [
        _FLATPAK, "run", "--command=chromium",
        "io.github.ungoogled_software.ungoogled_chromium",
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-site-isolation-trials",
        "--disable-extensions",
        "--disable-background-networking",
        "--js-flags=--max-old-space-size=256",
        "--remote-debugging-port=9222",
        f"--user-data-dir={BH_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    log.info("BrowserFetch: lancement de l'instance headless Ungoogled (CDP 9222)")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cdp_alive():
            log.info("BrowserFetch: CDP 9222 actif")
            return
        time.sleep(0.5)
    raise RuntimeError("BrowserFetch: le navigateur headless ne répond pas sur 9222")


# Script exécuté par la CLI browser-harness (helpers pré-importés : new_tab,
# js, wait_for_load, ...). Placeholders : __URL__ (url JSON), __API_KEY__ (clé
# CapBypass). Le token tourne dans le même process, injecté via json.dumps.
_CAPBYPASS_FUNC = r'''
import json, time, urllib.request

def _post(url, payload, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0 Safari/537.36",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def _capbypass(task_type, website_url, website_key):
    key = __API_KEY__
    resp = _post(__BASE__ + "/createTask", {
        "clientKey": key,
        "task": {"type": task_type, "websiteURL": website_url, "websiteKey": website_key},
    })
    if resp.get("errorId", 1) or not resp.get("taskId"):
        raise RuntimeError(f"CapBypass createTask failed: {resp.get('errorDescription', resp)}")
    task_id = resp["taskId"]
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(1)
        rr = _post(__BASE__ + "/getTaskResult",
                   {"clientKey": key, "taskId": task_id})
        status = rr.get("status")
        if status == "ready":
            token = rr["solution"].get("token", "")
            if token:
                return token
        elif status == "failed" or rr.get("errorId"):
            raise RuntimeError(f"CapBypass failed: {rr.get('errorDescription', rr)}")
    raise RuntimeError("CapBypass getTaskResult timeout")
'''



# Boucle agent unique : le LLM (__MODEL__) pilote le navigateur via les helpers
# browser-harness (new_tab, js, wait_for_load, page_info) en appelant __LLM_URL__
# avec function-calling (llama-server / proxy-switch :8080). Sort : un JSON
# {"ok", "content"/"error"} sur le dernier token de sortie stdout. Le LLM décide
# intelligent si le contenu est extractable (cookies -> accepte, captcha widget ->
# CapBypass) ou un échec (login/paywall/captcha irrésoluble -> done(content:"")).
_AGENT_SYSTEM = (
    "Tu pilotes un vrai navigateur pour extraire le contenu complet d'un article de presse.\n"
    "Objectif : retourner par done({\"content\": ...}) LE TEXTE RÉEL de l'article (paragraphes "
    "du corps), jamais une bannière, un mur de connexion ou un captcha.\n"
    "Règles (observe l'état réel de la page à chaque étape) :\n"
    "1. Commence par page_info() puis get_text() pour voir ce qui est réellement affiché.\n"
    "2. BANNIÈRE DE COOKIES / CONSENTEMENT : si la page affiche une bannière "
    "('nous utilisons des cookies', 'we use cookies', 'consentement', 'accepter', 'autoriser tout', "
    "'tout accepter', 'accept all', 'j'accepte'), cette bannière N'EST PAS l'article. Clique avec "
    "js() sur le bouton qui débloque le contenu (choisis intelligemment selon la langue : "
    "'Autoriser tout', 'Tout accepter', 'Accept all', 'Accepter', 'J'accepte' — chercher via "
    "Array.from(document.querySelectorAll('button')).find(b=>(b.innerText||'').trim()...)), attends, "
    "puis RE-observe et ré-extrais le vrai contenu.\n"
    "3. ÉCRAN DE CONSENTEMENT GOOGLE : si l'hostname est consent.google.com, clique via js() sur "
    "'Tout refuser' ou 'Tout accepter' (premier bouton trouvé par texte), attends la redirection "
    "vers la vraie page de l'article, puis continue.\n"
    "4. CAPTCHA RÉSOLUBLE (widget) : si tu détectes un widget reCAPTCHA ou Turnstile "
    "([data-sitekey], .g-recaptcha, .cf-turnstile, input[name=cf-turnstile-response]), appelle "
    "solve_captcha(sitekey, kind, url), puis injecte le token via js() dans l'input du widget "
    "et attends. Ce n'est pertinent que pour un widget bien présent.\n"
    "5. ÉCHEC NON CONTOURNABLE : si le contenu est bloqué de façon infranchissable, appelle "
    "done({\"content\": \"\"}) pour signaler qu'il n'est PAS extractable. Cas typiques :\n"
    "   a) Mur de LOGIN / inscription requise ('log in', 'sign in', 'create account', "
    "'please verify your email', 'please create a username', 'login pour continuer', "
    "'membership required', 'subscribe to continue') — pas de clic simple qui débloque.\n"
    "   b) CAPTCHA anti-bot structurel sans widget résoluble (DataDome / PerimeterX : "
    "iframe 'geo.captcha-delivery.com', 'DataDome CAPTCHA', ou managed challenge Cloudflare "
    "sans [data-sitekey]) — après 1-2 tentatives.\n"
    "   c) Paywall strict sans bouton de consentement qui redonne l'accès.\n"
    "6. Extraction : cible le sélecteur de contenu principal avec js() (ex: "
    "document.querySelector('article'), '.post-content', '.entry-content', '[itemprop=articleBody]') "
    "et lis son innerText — préfère le corps de l'article au body entier.\n"
    "7. done({\"content\": \"<texte>\"}) UNIQUEMENT si tu as >= 200 caractères utiles du VRAI article "
    "(corps réel), sans boilerplate, sans bannière. Sinon done({\"content\": \"\"}).\n"
    "Ne réponds jamais en prose : travaille UNIQUEMENT via les appels d'outils."
)

_AGENT_SCRIPT = r'''
import json, time, httpx

MAX_STEPS = __STEPS__
LLM_URL = __LLM_URL__
MODEL = __MODEL__
MAX = __MAX__
TARGET = __URL__

TOOLS = [
    {"type": "function", "function": {
        "name": "page_info", "description": "État courant de la page : url, titre, host.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_text", "description": "innerText courant du document (tronqué).",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "js", "description": "Exécute du JS dans la page et renvoie le résultat.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Code JavaScript (expression ou IIFE)"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "wait", "description": "Attend ms millisecondes (reload si besoin).",
        "parameters": {"type": "object", "properties": {
            "ms": {"type": "integer", "description": "Millisecondes", "default": 2500}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "solve_captcha", "description": "Résout un widget reCAPTCHA v2 ou Turnstile via CapBypass et renvoie le token.",
        "parameters": {"type": "object", "properties": {
            "sitekey": {"type": "string", "description": "Site key du widget (data-sitekey)"},
            "kind": {"type": "string", "description": "recaptcha|turnstile", "default": "recaptcha"},
            "url": {"type": "string", "description": "URL de la page (défaut: page courante)"}},
            "required": ["sitekey"]}}},
    {"type": "function", "function": {
        "name": "done", "description": "Termine avec le contenu final extrait.",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "Texte complet de l'article"}},
            "required": ["content"]}}},
]

def _js_str(code):
    try:
        r = js(code)
    except Exception as e:
        return {"error": str(e)[:400]}
    if isinstance(r, str):
        return {"result": r[:6000]}
    try:
        return {"result": json.dumps(r, ensure_ascii=False, default=str)[:6000]}
    except Exception:
        return {"result": str(r)[:6000]}

def run_tool(name, args):
    if name == "page_info":
        try:
            p = page_info() or {}
        except Exception as e:
            return {"error": f"page_info: {e}"}
        if isinstance(p, dict):
            return {"ok": True, "url": str(p.get("url", ""))[:300],
                    "title": str(p.get("title", ""))[:300]}
        return {"ok": True, "info": str(p)[:600]}
    if name == "get_text":
        r = _js_str("document.body ? document.body.innerText.slice(0, 9000) : ''")
        return {"ok": r.get("error") is None, **r}
    if name == "js":
        return _js_str(args.get("code", ""))
    if name == "wait":
        ms = int(args.get("ms", 2500))
        time.sleep(max(0, min(ms, 30000)) / 1000.0)
        try:
            wait_for_load(timeout=5)
        except Exception:
            pass
        return {"ok": True, "waited_ms": ms}
    if name == "solve_captcha":
        sitekey = args.get("sitekey", "")
        kind = args.get("kind", "recaptcha") or "recaptcha"
        url = args.get("url")
        if not url:
            try:
                url = js("location.href")
            except Exception:
                url = None
        url = url or TARGET
        task_type = {"recaptcha": "ReCaptchaV2TaskProxyLess",
                     "turnstile": "TurnstileTaskProxyLess"}.get(kind, kind)
        try:
            token = _capbypass(task_type, url, sitekey)
            return {"ok": True, "token": str(token)[:200]}
        except Exception as e:
            return {"ok": False, "error": str(e)[:400]}
    if name == "done":
        return {"ok": True}
    return {"error": f"unknown tool {name}"}

def main():
    # Nettoyage : fermer tous les onglets résiduels (profil/heredocs précédents)
    # pour que page_info/current ciblent bien le nouvel onglet cible.
    try:
        for _t in (list_tabs() or []):
            _tid = _t.get("targetId") or _t.get("target_id") or _t.get("id")
            if _tid:
                try:
                    close_tab(_tid)
                except Exception:
                    pass
        time.sleep(0.5)
    except Exception:
        pass
    try:
        new_tab(TARGET)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"init: {e}"}))
        return
    try:
        wait_for_load(timeout=20)
    except Exception:
        pass
    # Traversée automatique de l'écran de consentement Google (consent.google.com) :
    # cliquer "Tout refuser" puis attendre la redirection vers la vraie page.
    # Ces lignes évitent d'extraire la page de consentement comme si c'était l'article.
    try:
        _h0 = (js("location.hostname") or "")
        if "consent.google.com" in _h0:
            _ck = js("(()=>{const b=Array.from(document.querySelectorAll('button')).find(x=>(x.innerText||'').trim()==='Tout refuser');if(!b)return false;b.click();return true;})()")
            if _ck:
                time.sleep(4)
                try:
                    wait_for_load(timeout=20)
                except Exception:
                    pass
                time.sleep(2)
    except Exception:
        pass
    # Laisser le proof-of-work Cloudflare se stabiliser (sinon on déclare trop tôt
    # un challenge alors que la page aurait pu se résoudre seule).
    time.sleep(6)
    try:
        wait_for_load(timeout=10)
    except Exception:
        pass
    # Pré-abandon : challenge anti-bot PUIS fenêtre dépassée et sans widget
    # résoluble (reCAPTCHA/Turnstile) → inutile de faire tourner l'agent LLM.
    low0 = (js("document.title") or "") + " " + (js("document.body.innerText.slice(0,2000)") or "")
    low0 = low0.lower()
    if any(m in low0 for m in (
        "un instant", "verification de securite", "just a moment", "ray id",
        "verify you are not a bot", "verifying you are not a bot", "checking your browser",
    )):
        try:
            sitekey = js("(()=>{const e=document.querySelector('[data-sitekey],[data-turnstile],.cf-turnstile,#g-recaptcha');return e?(e.getAttribute('data-sitekey')||e.getAttribute('data-turnstile')||e.dataset.sitekey||''):''})()")
        except Exception:
            sitekey = ""
        if not sitekey:
            print(json.dumps({"ok": False, "error": "captcha challenge sans widget résoluble"}))
            return
    messages = [
        {"role": "system", "content": __SYSTEM__},
        {"role": "user", "content": "URL cible à extraire : " + TARGET},
    ]
    for step in range(1, MAX_STEPS + 1):
        try:
            with httpx.Client(timeout=300) as c:
                rr = c.post(LLM_URL, json={
                    "model": MODEL, "messages": messages,
                    "tools": TOOLS, "temperature": 0.2,
                })
                rr.raise_for_status()
                choice = rr.json()["choices"][0]
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"llm_call: {e}", "steps": step}))
            return
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
            for tc in tool_calls[:2]:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                import sys as _sys
                print(f"[agent:{step}] {name} {json.dumps(args, ensure_ascii=False)[:160]}", file=_sys.stderr)
                result = run_tool(name, args)
                if name == "done":
                    content = args.get("content") or ""
                    content = content.strip()
                    ok = len(content) > 100
                    print(json.dumps({"ok": ok, "content": content[:MAX],
                                      "steps": step, "tool": "done"}))
                    return
                messages.append({"role": "tool",
                                 "tool_call_id": tc.get("id") or "",
                                 "content": json.dumps(result, ensure_ascii=False)[:8000]})
        elif msg.get("content"):
            content = (msg.get("content") or "").strip()
            print(json.dumps({"ok": len(content) > 100, "content": content[:MAX], "steps": step}))
            return
        else:
            print(json.dumps({"ok": False, "error": "réponse vide du LLM", "steps": step}))
            return
    print(json.dumps({"ok": False, "error": f"max steps ({MAX_STEPS}) atteint", "steps": MAX_STEPS}))

main()
'''


def _build_agent_script(url: str) -> str:
    script = (
        _CAPBYPASS_FUNC + _AGENT_SCRIPT
    ).replace("__URL__", json.dumps(url)).replace(
        "__API_KEY__", json.dumps(CAPBYPASS_API_KEY)
    ).replace("__SYSTEM__", json.dumps(_AGENT_SYSTEM)).replace(
        "__LLM_URL__", json.dumps(LLM_URL)
    ).replace("__MODEL__", json.dumps(BH_AGENT_MODEL)).replace(
        "__STEPS__", str(BH_AGENT_STEPS)
    ).replace("__MAX__", str(_ARTICLE_CONTENT_MAX))
    return script.replace("__BASE__", json.dumps(CAPBYPASS_BASE_URL))


def _run_harness(script: str, timeout: float) -> dict:
    """Exécute un heredoc browser-harness et renvoie le dernier JSON stdout."""
    env = dict(os.environ)
    env["BU_NAME"] = BU_NAME
    env["BU_CDP_URL"] = f"http://{BH_CDP}"
    try:
        proc = subprocess.run(
            [BH_BIN],
            input=script.encode("utf-8"),
            capture_output=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"browser harness timeout ({timeout}s)"}
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    if err:
        for _l in err.splitlines()[-12:]:
            if _l.strip():
                log.info(f"BrowserFetch(stderr): {_l[:400]}")
    lines = [l for l in out.splitlines() if l.strip()]
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"ok": False, "error": err[:400] or f"exit {proc.returncode}"}


def fetch_article_with_agent(url: str) -> dict:
    """Boucle agentique unique : qwen-opus pilote browser-harness (function-calling)
    pour extraire le contenu. Le LLM décide intelligemment : il clique sur les bannières
    de cookies pour débloquer, appelle CapBypass (solve_captcha) si un widget
    reCAPTCHA/Turnstile est pertinent, et renvoie done({\"content\": \"\"}) s'il juge le
    contenu non extractable (mur de login, captcha irrésoluble, paywall)."""
    ensure_headless_browser()
    return _run_harness(_build_agent_script(url), BH_AGENT_TIMEOUT)


_DETERMINISTIC_SCRIPT = r'''
import json, time

TARGET = __URL__

try:
    new_tab("about:blank")
    time.sleep(1)
    goto_url(TARGET)
    wait_for_load(timeout=12)
    # Laisse tomber les challenges anti-bot résiduels (Cloudflare "just a moment").
    time.sleep(3)
    sel = js("(function(){var sels=['article','[itemprop=articleBody]','.post-content','.entry-content','.article-content','main','#content'];" +
             "for(var i=0;i<sels.length;i++){var el=document.querySelector(sels[i]);if(el&&(el.innerText||'').trim().length>200)return sels[i];}return '';})()")
    if sel:
        txt = js("(document.querySelector(%s).innerText||'').trim()" % (json.dumps(sel)))
    else:
        # Fallback : texte lisible du body entier nettoyé.
        txt = js("(function(){var t=(document.body.innerText||'').trim();return t;})()")
    txt = (txt or "").strip()
    if len(txt) >= 200:
        print(json.dumps({"ok": True, "content": txt[:__MAX__]}))
    else:
        print(json.dumps({"ok": False, "error": "short content", "content_len": len(txt)}))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)[:300]}))
'''


def _deterministic_extract(url: str) -> dict:
    """Passe déterministe (sans LLM) : ouvre la page et extrait le contenu via les
    sélecteurs d'article usuels, sinon `body.innerText`. Rapide et économe ; ne
    gère pas les bannières de cookies ni les captchas (pour ça on bascule sur
    l'agent LLM)."""
    ensure_headless_browser()
    script = _DETERMINISTIC_SCRIPT.replace("__URL__", json.dumps(url)).replace(
        "__MAX__", str(_ARTICLE_CONTENT_MAX))
    return _run_harness(script, BH_TIMEOUT)


def fetch_article_hybrid(url: str) -> dict:
    """Récupère le contenu : passe déterministe rapide (sélecteurs + innerText de
    body) en premier, sinon boucle agentique LLM. Le node BrowserFetchArticleNode
    déclenche le rebouclage ActuFinder (retry_fetch -> random_feed) en cas d'échec."""
    res = _deterministic_extract(url)
    if res.get("ok") and res.get("content", "").strip():
        return {"ok": True, "content": res["content"]}
    return fetch_article_with_agent(url)


async def fetch_article_async(url: str) -> dict:
    """Version async (non bloquante pour l'event loop du daemon)."""
    return await asyncio.to_thread(fetch_article_hybrid, url)


# ---------------------------------------------------------------------------
# P1 — Résolution du lien court Google News vers l'URL réelle de l'article.
# Google News renvoie les articles via des liens courts `news.google.com/rss/
# articles/CBMi...` qui se règlent sur un consent wall + une SPA. r.jina.ai
# est alors bloqué (403 Cloudflare) et l'agent navigateur galère. On résout
# donc d'abord ce lien court vers l'URL réelle du site source, sur laquelle
# r.jina.ai et le navigateur fonctionnent correctement.
# ---------------------------------------------------------------------------

_RESOLVER_SCRIPT = r'''
import json, time

TARGET = __URL__

GOOG = ("google.com", "gstatic.com", "ggpht.com", "googleusercontent.com")

def _is_garbage_link(h):
    # Lien Google / politique / bannière : jamais l'article cible.
    h = (h or "").lower()
    if not h.startswith(("http://", "https://")):
        return True
    if any(g in h for g in GOOG):
        return True
    path = h.split("://", 1)[-1]
    path = path.split("?", 1)[0]
    path = path.split("#", 1)[0]
    p = path.split("/", 1)[1] if "/" in path else ""
    if any(m in p for m in ("cookie", "privacy", "consent", "term", "policies",
                            "technologies", "about", "support", "accounts",
                            "legal", "licence", "license", "advertising")):
        return True
    return False

try:
    new_tab("about:blank")
    time.sleep(1)
    goto_url(TARGET)
    wait_for_load(timeout=15)
    # Laisser la SPA Google News se stabiliser (sinon on lit un consent wall /
    # une page transitoire).
    time.sleep(4)
    info = page_info()
    cur = info.get("url", "") or ""
    # Passe 1 : l'URL courante est déjà l'article réel (hors domaine Google).
    if cur and cur.lower().startswith(("http://", "https://")) and not _is_garbage_link(cur):
        print(json.dumps({"ok": True, "url": cur, "title": info.get("title", "")}))
        return
    # Passe 2 : traverser le consent wall Google si on y est, puis relire l'URL.
    try:
        _h = (js("location.hostname") or "")
        if "consent.google.com" in _h or "consent.google.co" in _h:
            js("(function(){var b=Array.from(document.querySelectorAll('button')).find(function(x){return /tous refuser|reject all|tout accepter|accept all/i.test((x.innerText||'').trim());});if(b){b.click();return true;}return false;})()")
            time.sleep(4)
            try:
                wait_for_load(timeout=15)
            except Exception:
                pass
            time.sleep(3)
            info = page_info()
            cur = info.get("url", "") or ""
            if cur and cur.lower().startswith(("http://", "https://")) and not _is_garbage_link(cur):
                print(json.dumps({"ok": True, "url": cur, "title": info.get("title", "")}))
                return
    except Exception:
        pass
    # Passe 3 : chercher le lien externe réel dans le DOM (en excluant TOUS les
    # liens Google et les pages "cookie/privacy/terms"), préférer l'URL du lien
    # dont le texte est explicite, sinon le dernier lien externe valide.
    ext = ""
    try:
        ext = js("(function(){var golden=['google.com','gstatic.com','ggpht.com','googleusercontent.com'];var badpath=['cookie','privacy','consent','term','policies','technologies','about','support','accounts','legal','licence','license','advertising'];function isGarbage(h){if(!/^https?:\\/\\//.test(h))return true;var lo=h.toLowerCase();for(var k=0;k<golden.length;k++){if(lo.indexOf(golden[k])>=0)return true;}var path=lo.split('://')[1].split(/[?#]/)[0];var seg=path.split('/').slice(1).join('/');for(var j=0;j<badpath.length;j++){if(seg.indexOf(badpath[j])>=0)return true;}return false;}var els=document.querySelectorAll('a');var out='';for(var i=0;i<els.length;i++){var h=els[i].href||'';if(!isGarbage(h)){var t=(els[i].innerText||'').trim();if(t.length>4){return h;}out=h;}}return out;})()")
    except Exception:
        ext = ""
    # (gardé simple : en cas d'échec JS on laisse l'URL courante)
    final = ext or (cur if cur.lower().startswith(("http://", "https://")) else "")
    print(json.dumps({"ok": bool(final), "url": final, "title": info.get("title", "")}))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)[:300], "url": ""}))
'''


def _is_google_news(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return "news.google.com" in host or "consent.google.com" in host


def resolve_short_url(url: str) -> dict:
    """Résout un lien court Google News vers l'URL réelle de l'article.

    Retourne {"ok": bool, "url": str, "title": str|""} — si l'URL n'est pas un
    lien Google News ou en cas d'échec, renvoie {"ok": False, "url": url} pour
    conserver le comportement actuel (on feed l'URL d'origine).
    """
    if not _is_google_news(url):
        return {"ok": False, "url": url}
    try:
        ensure_headless_browser()
    except Exception:
        return {"ok": False, "url": url}
    script = _RESOLVER_SCRIPT.replace("__URL__", json.dumps(url))
    # browser-harness exécute un corps top-level : `return` hors fonction est un
    # SyntaxError. On indente tout le corps sous `def main()` et on l'appelle.
    body = "\n".join(("    " + l if l.strip() else l) for l in script.splitlines())
    body = "def main():\n" + body + "\nmain()\n"
    result = _run_harness(body, BH_AGENT_TIMEOUT)
    if result.get("ok") and result.get("url"):
        return {"ok": True, "url": result["url"], "title": result.get("title", "")}
    return {"ok": False, "url": url}


async def resolve_short_url_async(url: str) -> dict:
    """Version async (non bloquante pour l'event loop du daemon)."""
    return await asyncio.to_thread(resolve_short_url, url)