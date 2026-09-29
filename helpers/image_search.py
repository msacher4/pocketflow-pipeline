"""Recherche d'image réelle via Bing Images + téléchargement.

Utilisé par RealCharacterImageNode (chemin alt) : ouvre Bing Images dans
l'instance headless browser-harness, extrait les candidats structurés
(murl/title/page), puis télécharge binaire via HTTP standard.

Google Images est volontairement évité : son anti-bot redirige systématiquement
les clients headless vers /sorry (CAPTCHA). Bing est nettement plus permissif.
"""
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

from config_actufinder import BH_BIN, BH_CDP, BH_PROFILE

log = logging.getLogger("pocketflow-pipeline")

BU_NAME = "pf_browser"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0 Safari/537.36"
)

# Script de la CLI browser-harness : ouvre Bing Images, attend le chargement,
# extrait jusqu'à LIMIT candidats de type {murl, turl, title, page} via JS.
_BING_EXTRACT = r'''
import json, time, urllib.parse

TARGET = __URL__
LIMIT = __LIMIT__
new_tab(TARGET)
try:
    wait_for_load(timeout=20)
except Exception:
    pass
time.sleep(4)
r = js("""
(()=>{
  const out=[];
  for (const a of document.querySelectorAll('a.iusc')) {
    try { const d=JSON.parse(a.getAttribute('m')||'{}');
      if(d.murl) out.push({murl:d.murl, turl:d.turl||'', title:d.t||'', page:d.purl||''});
    } catch(e){}
  }
  return out;
})()
""".strip())
if isinstance(r, dict) and r.get("error"):
    print(json.dumps({"ok": False, "error": r["error"]}))
else:
    print(json.dumps({"ok": True, "candidates": (r or [])[: LIMIT]}))
'''


def _run_extract(url: str, limit: int) -> dict:
    env = dict(os.environ)
    env["BU_NAME"] = BU_NAME
    env["BU_CDP_URL"] = f"http://{BH_CDP}"
    script = _BING_EXTRACT.replace("__URL__", json.dumps(url)) \
                          .replace("__LIMIT__", str(limit))
    try:
        proc = subprocess.run([BH_BIN], input=script.encode("utf-8"),
                              capture_output=True, env=env, timeout=90)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "browser harness timeout"}
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    for line in reversed(out.splitlines()):
        if not line.strip():
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"ok": False, "error": proc.stderr.decode("utf-8", errors="replace")[:300]}


def search_image_candidates(terms: str, limit: int = 15) -> list[dict]:
    """Retourne les candidats d'image Bing pour `terms` (vide si échec)."""
    url = ("https://www.bing.com/images/search?q="
           + urllib.parse.quote(terms))
    data = _run_extract(url, limit)
    if not data.get("ok"):
        log.warning(f"image_search: {data.get('error')}")
        return []
    cands = data.get("candidates") or []
    # Filtrage : on garde les URLs http(s) potentiellement directes, on retire
    # les microVignettes Bing (mm.bing.net) et les blobs/placeholders.
    ok = []
    seen = set()
    for c in cands:
        murl = (c.get("murl") or "").strip()
        if not re.match(r"^https?://", murl):
            continue
        if "mm.bing.net" in murl or "blob:" in murl:
            continue
        if murl in seen:
            continue
        seen.add(murl)
        ok.append({"murl": murl, "title": c.get("title", ""), "page": c.get("page", "")})
        if len(ok) >= limit:
            break
    return ok


def download_image(url: str, dest_path: str, timeout: int = 40) -> str | None:
    """Télécharge l'image `url` vers `dest_path` et retourne le chemin ou None."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
        "Referer": "https://www.bing.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except Exception as e:
        log.warning(f"image_search: téléchargement échoué {url[:80]}: {e}")
        return None
    if not raw or len(raw) < 500:
        log.warning(f"image_search: contenu image trop petit/absent ({len(raw)} octets)")
        return None
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(raw)
    return dest_path
