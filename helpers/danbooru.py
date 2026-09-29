"""Recherche d'images réelles de personnages via l'API Danbooru.

Source retenue pour le chemin alt (I2V par image réelle) : Danbooru fournit de
nombreux visuels (fan-art / rendus) pour les personnages d'anime/jeux/séries,
avec une classification de modération fiable. Pour publication, on ne garde
JAMAIS que les posts `rating:g` (general/SFW) — filtré en dur dans la requête.

API : GET https://danbooru.donmai.us/posts.json?tags=<tags>&limit=N
Résolution de tag : les tags sont en snake_case avec l'oeuvre entre parenthèses
(ex. `firefly_(honkai:_star_rail)`). On tente plusieurs variantes car l'ordre du
nom et la ponctuation de la franchise peuvent varier (ex. `shiranui_mai`).
"""
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger("pocketflow-pipeline")

BASE = "https://danbooru.donmai.us"
CDN = "https://cdn.donmai.us"
_UA = "PocketFlowPipeline/1.0 (research; contact: marcs)"

# Mots totalement interdits dans un tag : on ne contient jamais de caractères
# dangereux, on accepte lettres/chiffres/underscore plus la notation oeuvre.
_TAG_RE = re.compile(r"^[a-z0-9_]+$")


def _clean_token(s: str) -> str:
    """Normalise un mot en token snake_case Danbooru (minuscules, _, :→ _).

    Les tirets `-` sont CONSERVÉS : de nombreux noms de personnages en portent
    (ex. `Cha Hae-in` → `cha_hae-in`, tag Danbooru réel). Les espaces, `:`, `.`,
    `/` et apostrophes deviennent des underscores.
    """
    if not s:
        return ""
    s = re.sub(r"[:\s/.']+", "_", s.lower())
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s.strip("_-")


def build_tags(character: dict) -> list[str]:
    """Construit la liste de tags Danbooru à essayer, du plus précis au plus laxiste.

    Ex. character {name: "Firefly", franchise: "Honkai: Star Rail"} ->
      - firefly_(honkai:_star_rail)
      - firefly_honkai_star_rail
      - firefly
    """
    name = _clean_token(character.get("name", ""))
    franchise = _clean_token(character.get("franchise", ""))
    if not name:
        return []
    tags = []
    if franchise:
        tags.append(f"{name}_({franchise})")
        tags.append(f"{name}_{franchise}")
    tags.append(name)
    # Nom d'origine inversé (ordre prénom/nom japonais), ex. shiranui_mai
    parts = name.split("_")
    if len(parts) >= 2:
        rev = "_".join(reversed(parts))
        if franchise:
            tags.append(f"{rev}_({franchise})")
        tags.append(rev)
    # Variante wildcard : si la franchise exacte ne matche pas (ex. tag Danbooru
    # `lux_(league_of_legends)` vs franchise « 2XKO »), on tente `nom_*` pour
    # découvrir la vraie franchise. Un filtre strict côté requête évite que des
    # persos homonymes (`luxio`, `luxury_ball`...) polluent les résultats.
    tags.append(f"{name}_*")
    # retourne sans doublons, en gardant l'ordre
    seen = set()
    out = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def search_posts(character: dict, limit: int = 12) -> list[dict]:
    """Retourne les posts rating:g pour le personnage, toutes variantes de tag
    confondues (dédupliquées par URL). Vide si aucun résultat."""
    results = []
    seen_ids = set()
    tags = build_tags(character)
    name = _clean_token(character.get("name", ""))
    for tag in tags:
        is_wildcard = tag.endswith("_*")
        # Un '-' dans un tag (ex. `cha_hae-in`) est pour Danbooru un opérateur
        # d'exclusion s'il reste brut : on l'encode en %2D pour qu'il fasse
        # partie intégrante du tag. `quote` laisse '-' identique et réserverait
        # '%' — on encode donc le '-' APRÈS le quote.
        encoded_tag = urllib.parse.quote(tag, safe="").replace("-", "%2D")
        query = f"{encoded_tag}%20rating%3Ag"
        url = f"{BASE}/posts.json?tags={query}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            log.warning(f"danbooru: requête échouée pour '{tag}': {e}")
            time.sleep(0.6)
            continue
        if not isinstance(data, list):
            continue
        for p in data:
            pid = p.get("id")
            if pid in seen_ids:
                continue
            if is_wildcard:
                # Un résultat wildcard n'est pertinent que s'il porte réellement
                # un tag commençant par `nom_` (vrai perso, même franchise
                # inconnue). Les homonymes occasionnels (objet, autre perso)
                # portent d'autres tags.
                tag_string = p.get("tag_string", "")
                relevant = any(t.startswith(f"{name}_") for t in tag_string.split())
                if not relevant:
                    continue
            seen_ids.add(pid)
            file_url = p.get("file_url") or p.get("large_file_url") or ""
            if not file_url:
                continue
            results.append({
                "id": pid,
                "file_url": file_url,
                "rating": p.get("rating", ""),
                "w": p.get("image_width", 0),
                "h": p.get("image_height", 0),
                "tags": p.get("tag_string", "")[:300],
            })
        if results:
            # On a trouvé des résultats pour cette variante de tag : inutile de
            # continuer vers des variantes plus laxistes.
            break
        time.sleep(0.6)
    return results


def download_image(url: str, dest_path: str, timeout: int = 40) -> str | None:
    """Télécharge l'image `url` vers `dest_path`, retourne le chemin ou None."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*,*/*;q=0.8",
        "Referer": f"{BASE}/",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except Exception as e:
        log.warning(f"danbooru: téléchargement échoué {url[:70]}: {e}")
        return None
    if not raw or len(raw) < 500:
        log.warning(f"danbooru: contenu image trop petit/absent ({len(raw)} octets)")
        return None
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(raw)
    return dest_path
