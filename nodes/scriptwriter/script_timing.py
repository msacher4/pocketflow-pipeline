import re

from config import SDCPP_FRAMES, SDCPP_FPS, SDCPP_I2V_FRAMES
from helpers.i2v_slots import I2V_SLOT_COUNT

# Les clips sont TOUJOURS générés au même fps réel passé à sd-cli (SDCPP_FPS=24).
# L'I2V ne fait PAS exception : SDCPP_I2V_FRAMES=97 @ 24fps = 4.04s, donc I2V_SEC
# == T2V_SEC == 4s. Les deux modes partagent le même build LTX-2.5 (mêmes 97
# frames, même fps), ils ne diffèrent que par l'image source.
# NE PAS diviser par un FPS codé en dur ici : c'est exactement l'erreur qui
# annonçait 6s (97/16) pour un clip de 4s, et qui faisait rejeter au validateur de
# timing des scripts dont les horaires étaient justes.
I2V_SEC = int(round(SDCPP_I2V_FRAMES / SDCPP_FPS))   # ≈ 4s (97 frames @24fps)
T2V_SEC = int(round(SDCPP_FRAMES / SDCPP_FPS))       # ≈ 4s (97 frames @24fps)

# FPS Historique (16.0) : un vestige de la période où l'I2V valait 49 frames. Plus
# utilisé pour le calcul de durée, conservé pour les imports existants.
FPS = 16.0

# RÈGLE UNIQUE (helpers.i2v_slots) : l'I2V est la 1re vidéo de chacun des
# I2V_SLOT_COUNT premiers plans qui ont AU MOINS une vidéo. Une 2e vidéo de
# plan (VO longue au hook) reste donc un T2V, même placée ENTRE les deux I2V.
# I2V_PLANS ci-dessous n'est plus qu'un REPLI pour les appels sans parse_plans
# (tests, constructions manuelles de plans) : ne pas s'en servir ailleurs.
I2V_PLANS = (1, 2)

_PLAN_TITLE_RE = re.compile(
    r"^\s*[-*]?\s*Plan\s*(\d+)\s*\(\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*s?\s*\)",
    re.IGNORECASE,
)
_EDIT_PLAN_RE = re.compile(r"^\s*[-*]?\s*Plan\s*(\d+)\s+", re.IGNORECASE)
_EDIT_LINE_RE = re.compile(r"^\s*[-*]?\s*(video|vo|audio)\s*:\s*(.+)$", re.IGNORECASE)
_HEADER_RE = re.compile(
    r"^\s*(#{1,6})\s*(.*?)\s*(?:\(\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*s?\s*\))?\s*$",
    re.IGNORECASE,
)
_VIDEO_RE = re.compile(r"^\s*[-*]?\s*Video\s*:\s*(.+)$", re.IGNORECASE)
_VO_RE = re.compile(r"^\s*[-*]?\s*VO\s*:\s*(.+)$", re.IGNORECASE)

_SECTION_NAMES = {
    "hook": "hook", "intro": "hook",
    "body": "body",
    "cta": "cta", "outro": "cta", "end": "cta",
}


def _norm_section(raw: str) -> str:
    key = str(raw or "body").strip().lower()
    key = key.split("(")[0].strip()
    for s, v in _SECTION_NAMES.items():
        if s in key:
            return v
    return "body"


def asset_kind(plan_num: int, video_idx_in_plan: int) -> str:
    """REPLI historique : I2V pour la 1re vidéo des plans 1 et 2. Utilise
    `asset_kind_of` dès que le plan vient de `parse_plans` (règle unique)."""
    if video_idx_in_plan == 0 and plan_num in I2V_PLANS:
        return "i2v"
    return "t2v"


def asset_duration_secs(plan_num: int, video_idx_in_plan: int) -> int:
    return I2V_SEC if asset_kind(plan_num, video_idx_in_plan) == "i2v" else T2V_SEC


def annotate_i2v_assets(plans: list, count: int = I2V_SLOT_COUNT) -> set:
    """Pose `kind` (i2v/t2v) sur chaque ligne vidéo, via la règle UNIQUE.

    Renvoie l'ensemble des numéros de plans dont la 1re vidéo est un I2V.
    Appelé par `parse_plans` : tous les consommateurs (durée de plan, budget de
    mots, repair) lisent alors `v["kind"]` et ne peuvent plus diverger de ce que
    l'AssetPlanner marque `mode: i2v` ni de ce que le validateur JEV b-roll
    exempte. Sans cet alignement, un plan dont le Plan 1 n'a pas de vidéo était
    compté 6s (I2V) par le timing alors que le planner le produisait en T2V (4s).
    """
    order = [p["num"] for p in plans if p.get("video_lines")]
    i2v_plans = set(order[:count])
    for p in plans:
        is_i2v_plan = p["num"] in i2v_plans
        for v in p.get("video_lines") or []:
            v["kind"] = "i2v" if (is_i2v_plan and v.get("idx") == 0) else "t2v"
    return i2v_plans


def asset_kind_of(plan: dict, video: dict) -> str:
    """Type d'un asset d'après l'annotation de `parse_plans` (règle unique),
    avec repli sur l'ancienne règle locale si le plan n'a pas été annoté."""
    kind = (video or {}).get("kind")
    if kind in ("i2v", "t2v"):
        return kind
    return asset_kind(plan["num"], (video or {}).get("idx", 0))


def asset_duration_of(plan: dict, video: dict) -> int:
    """Durée d'un asset, cohérente avec l'annotation i2v/t2v."""
    return I2V_SEC if asset_kind_of(plan, video) == "i2v" else T2V_SEC


def parse_plans(script: str) -> list[dict]:
    """Parse les plans d'un script. Chaque plan: num, title_line, start_s/end_s
    déclarés, section, video_lines [{line, idx, text}], vo_lines [{line, text}]."""
    plans = []
    cur = None
    section = "body"
    for i, raw in enumerate((script or "").splitlines()):
        m = _HEADER_RE.match(raw)
        if m and m.group(2).strip() and raw.strip().startswith("#"):
            section = _norm_section(m.group(2))
            continue
        m = _PLAN_TITLE_RE.match(raw)
        if m:
            cur = {
                "num": int(m.group(1)),
                "title_line": i,
                "start_s": float(m.group(2)),
                "end_s": float(m.group(3)),
                "section": section,
                "video_lines": [],
                "vo_lines": [],
            }
            plans.append(cur)
            continue
        if cur is None:
            continue
        m = _VIDEO_RE.match(raw)
        if m:
            cur["video_lines"].append({
                "line": i,
                "idx": len(cur["video_lines"]),
                "text": m.group(1).strip(),
            })
            continue
        m = _VO_RE.match(raw)
        if m:
            cur["vo_lines"].append({"line": i, "text": m.group(1).strip()})
    annotate_i2v_assets(plans)
    return plans


def parse_section_headers(script: str) -> list[dict]:
    """En-têtes `### SECTION (S-Es)` : line, name, had_range, start_s/end_s."""
    out = []
    for i, raw in enumerate((script or "").splitlines()):
        m = _HEADER_RE.match(raw)
        if m and m.group(2).strip() and raw.strip().startswith("#"):
            out.append({
                "line": i,
                "name": m.group(2).strip(),
                "had_range": m.group(3) is not None,
                "start_s": float(m.group(3)) if m.group(3) else None,
                "end_s": float(m.group(4)) if m.group(4) else None,
            })
    return out


def plan_required_duration(plan: dict) -> float:
    """Durée d'un plan = somme des durées de ses assets (I2V≈4s, T2V≈4s)."""
    return sum(
        asset_duration_of(plan, v) for v in plan["video_lines"]
    )


def plan_budget_words(plan: dict) -> int:
    """Budget de parole d'un plan ≈ 2 mots/sec + filet +4, sur la durée des
    assets gardés (max 2 vidéos/plan). → 8s (2 assets) ≈ 20 mots, 4s (1 asset) ≈ 12."""
    kept = plan["video_lines"][:2]
    dur = max(sum(asset_duration_of(plan, v) for v in kept), 1.0)
    return int(dur * 2) + 4


def plan_vo_words(plan: dict) -> int:
    total = 0
    for vo in plan["vo_lines"]:
        total += len([w for w in vo["text"].replace("'", " ").split() if w.strip()])
    return total


def _count_words(text: str) -> int:
    """Comptage OFFICIEL du pipeline (contractions = 2 mots). Identique à
    plan_vo_words et au VoShortener."""
    return len([w for w in (text or "").replace("'", " ").split() if w.strip()])


_SENT_RE = re.compile(r"[^.!?]+[.!?]+")


def _shorten_vo_to_budget(text: str, budget: int) -> str:
    """Réduit une VO au budget par paliers (phrase complète → proposition →
    premiers mots), sans jamais couper un mot. Dernier palier = troncature aux
    premiers mots : tient toujours si budget >= 1."""
    if _count_words(text) <= budget:
        return text
    # palier 1 : retire la dernière phrase complète
    parts = [m.group(0).strip() for m in _SENT_RE.finditer(text)]
    if len(parts) > 1:
        text = " ".join(parts[:-1]).strip()
        if _count_words(text) <= budget:
            return text
    # palier 2 : retire la dernière proposition (virgule)
    if "," in text:
        cand = text.rsplit(",", 1)[0].rstrip()
        if cand.strip():
            text = cand
            if _count_words(text) <= budget:
                return text
    # palier 3 : premiers mots jusqu'au budget
    tokens = text.split()
    kept = []
    for w in tokens:
        if _count_words(" ".join(kept + [w])) > budget:
            break
        kept.append(w)
    return " ".join(kept).rstrip() if kept else text


def pacing_errors(script: str) -> list[str]:
    """Erreurs de timing/pacing d'un script: plus de 2 vidéos par plan, horaires
    ≠ Σ assets, VO en sur-budget. Message taggé 'VO trop longue' / 'somme de ses
    assets' / 'max 2 vidéos par plan'."""
    errs = []
    for p in parse_plans(script or ""):
        dur = plan_required_duration(p)
        if len(p["video_lines"]) > 2:
            errs.append(
                f"Trop de vidéos au Plan {p['num']} ({len(p['video_lines'])} lignes "
                f"Video: — max 2 par plan : I2V≈{I2V_SEC}s + T2V≈{T2V_SEC}s = {I2V_SEC + T2V_SEC}s). "
                "Retire les vidéos excédentaires au lieu d'en ajouter."
            )
        if p["video_lines"]:
            declared = p["end_s"] - p["start_s"]
            if abs(declared - dur) > 0.6:
                errs.append(
                    f"Les horaires du Plan {p['num']} ({p['start_s']:.0f}-{p['end_s']:.0f}s) "
                    f"ne correspondent pas à la somme de ses assets "
                    f"({len(p['video_lines'])} asset(s) → {dur:.0f}s attendu : "
                    f"I2V≈{I2V_SEC}s, T2V≈{T2V_SEC}s)."
                )
        words = plan_vo_words(p)
        budget = plan_budget_words(p)
        if words > budget:
            errs.append(
                f"VO trop longue pour la durée de son plan ({words} mots dans un "
                f"plan de {dur:.0f}s — max ≈ {budget} mots à 2 mots/sec): "
                f"'{p['vo_lines'][0]['text'][:70]}'. RACCOURCIS/SIMPLIFIE la VO à "
                f"≤ {budget} mots (2 vidéos par plan max), sans couper la phrase "
                "en deux plans."
            )
    return errs


# ---------------------------------------------------------------------------
# Règle 12 — DYNAMISME OBLIGATOIRE (source UNIQUE de vérité)
#
# Cette règle a vécu en regex dupliquée dans pydantic_validation.py ET
# script_fixer.py. Les deux copies ont divergé, et la version d'origine
# rejetait `frozen` sur TOUTE la ligne : un décor (« a vast frozen throne
# hall ») tuait un plan dont le personnage était en mouvement. Résultat : 6
# régénérations AltSG et ~39 min pour une erreur identique à chaque cycle
# (run 20261004_131919).
#
# Le décor peut être figé ; c'est le PERSONNAGE qui doit bouger. D'où les
# deux régimes :
#   1. phrases de pose statique → rejet inconditionnel (désambiguïsées) ;
#   2. personnage détecté mais aucun verbe dynamique → rejet.
# Une ligne purement décorative (règle 6 du soul : « personnages, décors,
# actions, caméra, éclairage, ambiance ») n'échoue PAS sur le point 2.
# ---------------------------------------------------------------------------

_STATIC_POSE_PHRASES = (
    r"standing still",
    r"static shot",
    r"static pose",
    r"stands alone",
    r"stands at the center",
    r"stands motionless",
    r"stands in place",
    r"remains still",
    r"remains motionless",
    r"holds still",
    r"waits motionless",
    r"freezes in place",
    r"frozen in place",
    r"poses still",
    # Poses statiques explicites : `fold\w*` est aussi dynamique ("folds shirts"
    # = lessive en action) mais "hands folded" est une pose immobile.
    r"hands? folded",
    r"arms? folded",
    r"arms? crossed",
    r"hands? clasped",
)
_STATIC_POSE_RE = re.compile(r"\b(?:" + "|".join(_STATIC_POSE_PHRASES) + r")\b", re.IGNORECASE)

# `frozen` et `motionless` seuls ne sont PLUS un rejet : ils qualifient le
# décor aussi bien que le personnage (« frozen battlefield », « cape
# motionless while she spins »). Ils ne sont invalides que via le point 2,
# c'est-à-dire quand aucun verbe dynamique n'est présent.
_PERSON_RE = re.compile(
    r"\b(?:"
    r"girl|boy|woman|man|lady|ladies|gentleman|damsel|maiden|queen|princess|king|"
    r"tsaritsa|tsarina|empress|emperor|swordswoman|swordsman|warrior|knight|"
    r"heroine|hero|mage|witch|wizard|ninja|samurai|assassin|spy|thief|rogue|"
    r"idol|dancer|model|streamer|gamer|chef|waitress|clerk|athlete|diver|"
    r"idol|fan|player|vlogger|influencer|doll|elf|fairy|catgirl|catboy|"
    r"female|male|ladyfriend|girlfriend|boyfriend|couple|"
    r"traveler|traveller|wanderer|archetype|"
    r"bystander|onlooker|passerby|peer|"
    r"she|her|hers|he|him|his|himself|herself|themselves"
    r")\b",
    re.IGNORECASE,
)

# Verbes d'action / de mouvement. Formes en -ing et en 3e personne privilégiées :
# "whipping", "billowing", "strides", "spins" sont dynamiques sans ambiguïté
# nominale, alors que "spin"/"draw" seuls seraient ambigus.
_DYNAMIC_VERB_RE = re.compile(
    r"\b(?:" + "|".join((
        r"accelerat\w*", r"advanc\w+", r"arch\w+", r"ascending", r"balancing",
        r"billow\w*", r"bowing", r"brandish\w*",
        r"burst\w*", r"cascad\w+", r"charg\w+", r"clutch\w*", r"coiling",
        r"cradl\w+", r"crouch\w*", r"curl\w+", r"cutt\w+", r"danc\w+",
        r"dart\w*", r"dash\w*", r"dodging", r"drift\w*", r"duel\w*",
        r"emerg\w+", r"explod\w+", r"extend\w*", r"fall\w*", r"flick\w*",
        r"flow\w*", r"flutter\w*", r"fold\w*", r"glid\w+",
        r"grasp\w*", r"gripp\w*", r"hoist\w*", r"hurrying",
        r"hurdling", r"juggling", r"jump\w*", r"kick\w*", r"kneel\w*",
        r"lanc\w+", r"leap\w*", r"lift\w*", r"march\w*", r"nod\w*",
        r"open\w*", r"plung\w+", r"plummet\w*", r"press\w+", r"prowling",
        r"pull\w*", r"pump\w*", r"rais\w+", r"reach\w+", r"recoil\w*",
        r"rippl\w+", r"roll\w*", r"rush\w*", r"sail\w*", r"scal\w+",
        r"scatter\w*", r"shatter\w*", r"sheath\w*",
        r"shift\w*", r"slash\w*", r"slid\w+", r"smil\w+",
        r"snarl\w*", r"soar\w*", r"spin\w*", r"sprawl\w*", r"stalk\w*",
        r"stamp\w*", r"stir\w*", r"stride\w*", r"strok\w+", r"strut\w*",
        r"surge\w*", r"sway\w*", r"swing\w*", r"swirl\w*", r"swerve\w*",
        r"tail\w*", r"tear\w*", r"thrust\w*", r"tilt\w*", r"topple\w*",
        r"turn\w*", r"twirl\w*", r"unfurl\w*", r"vault\w*", r"wade\w*",
        r"waver\w*", r"whip\w*", r"whirling", r"wing\w*", r"wrapping",
        r"yaw\w*",
    )) + r")\b",
    re.IGNORECASE,
)


def character_present(video_text: str) -> bool:
    """True si la ligne Video: décrit un personnage (nom commun ou pronom genré).

    Une ligne purement décorative/ambiantielle renvoie False : la règle 12
    n'exige alors AUCUN verbe (sinon on rejetait les plans d'ambiance que la
    règle 6 du soul autorise explicitement)."""
    return bool(_PERSON_RE.search(video_text or ""))


def has_dynamic_verb(video_text: str) -> bool:
    """True si la ligne contient au moins un verbe d'action / de mouvement."""
    return bool(_DYNAMIC_VERB_RE.search(video_text or ""))


def static_video_reason(video_text: str) -> str | None:
    """Raison de rejet d'une ligne Video: statique, ou None si elle est OK.

    Source UNIQUE utilisée par le validateur pydantic ET par le ScriptFixer
    (auto-validation de sortie) : impossible de diverger."""
    low = (video_text or "").lower()
    m = _STATIC_POSE_RE.search(low)
    if m:
        return (
            f"pose statique interdite ({m.group(0)!r}) — le personnage doit être "
            "en action ou en pose dynamique, pas figé"
        )
    if character_present(low) and not has_dynamic_verb(low):
        return (
            "aucun verbe d'action ni de mouvement sur le personnage — un asset "
            "vidéo doit décrire du mouvement (personnage en action ou pose "
            "dynamique, mouvement caméra, éléments en mouvement)"
        )
    return None


def plan_vo_refs(script: str) -> dict[int, list[str]]:
    """plan num -> [refs des VO du plan] (v1..vN, ordre global des lignes VO:)."""
    out: dict[int, list[str]] = {}
    g = 0
    for p in parse_plans(script or ""):
        ids = []
        for _ in p["vo_lines"]:
            g += 1
            ids.append(f"v{g}")
        if ids:
            out[p["num"]] = ids
    return out


def _fmt_plan_title(orig_line: str, num: int, start: float, end: float) -> str:
    stripped = orig_line.strip()
    prefix = ""
    if stripped[:2] in ("- ", "* "):
        prefix = stripped[:2]
        stripped = stripped[2:].strip()
    return f"{prefix}Plan {num} ({start:.0f}-{end:.0f}s)"


def _fmt_section_header(orig_line: str, start: float, end: float) -> str:
    m = _HEADER_RE.match(orig_line)
    if not m or not m.group(2).strip():
        return orig_line
    hashes = m.group(1)
    name = m.group(2).strip()
    return f"{hashes} {name} ({start:.0f}-{end:.0f}s)"


def apply_partial_edit(script: str, edit_text: str) -> str:
    """Applique une édition partielle au script existant.

    Format accepté (une ligne par correction) :
      Plan 3 VO:    nouveau texte de la VO
      Plan 3 Video: nouveau prompt de la 1re Video: du plan
    La première ligne du type demandé dans le plan est remplacée (ou ajoutée
    juste après le titre du plan si le plan n'en a pas). Les numéros de plans
    inconnus sont ignorés.

    Si aucune ligne ne démarre par 'Plan N ', l'edit est traité comme un script
    COMPLET (remplacement intégral)."""
    dirty_lines = [l for l in (edit_text or "").splitlines() if l.strip()]
    if not dirty_lines:
        return script

    partial = all(_EDIT_PLAN_RE.match(l) for l in dirty_lines)
    if not partial:
        return edit_text

    # Actions par plan : kind -> nouveau texte.
    repls: dict[int, dict[str, str]] = {}
    for l in dirty_lines:
        m = _EDIT_PLAN_RE.match(l)
        rest = l[m.end():].strip()
        lm = _EDIT_LINE_RE.match(rest)
        if not lm:
            continue
        num = int(m.group(1))
        kind = lm.group(1).lower()
        repls.setdefault(num, {})[kind] = lm.group(2).strip()
    if not repls:
        return script

    plans = parse_plans(script)
    lines = (script or "").splitlines()
    if not plans:
        return script

    # Audio: vit en tête de script (pas par plan) — remplacé globalement, 1re occurrence.
    for l in dirty_lines:
        m = _EDIT_PLAN_RE.match(l)
        rest = l[m.end():].strip()
        lm = _EDIT_LINE_RE.match(rest)
        if not lm or lm.group(1).lower() != "audio":
            continue
        for k, raw in enumerate(lines):
            if _EDIT_LINE_RE.match(raw) and _EDIT_LINE_RE.match(raw).group(1).lower() == "audio":
                lines[k] = f"Audio: {lm.group(2).strip()}"
                break

    bound_of = {}
    for i, p in enumerate(plans):
        bound_of[p["num"]] = plans[i + 1]["title_line"] if i + 1 < len(plans) else len(lines)

    out: list[str] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        cur = next((p for p in plans if p["title_line"] == i), None)
        if cur is None:
            out.append(raw)  # section headers, Audio:, lignes hors plan
            i += 1
            continue
        out.append(raw)
        num = cur["num"]
        actions = repls.get(num, {})
        done: set[str] = set()
        # corps du plan : remplace la 1re ligne du type demandé
        j = i + 1
        while j < bound_of[num]:
            lm = _EDIT_LINE_RE.match(lines[j])
            kind = lm.group(1).lower() if lm else None
            if kind in actions and kind not in done:
                prefix = {"vo": "VO", "video": "Video", "audio": "Audio"}[kind]
                out.append(f"{prefix}: {actions[kind]}")
                done.add(kind)
            else:
                out.append(lines[j])
            j += 1
        # types demandés mais absents du plan -> insérés après le titre
        for kind, text in actions.items():
            if kind == "audio" or kind in done:
                continue
            prefix = {"vo": "VO", "video": "Video", "audio": "Audio"}[kind]
            out.append(f"{prefix}: {text}")
        i = j  # corps déjà copié, on reprend après le plan

    return "\n".join(out)


def rewrite_plan_timestamps(script: str) -> str:
    """Recalcule les `Plan N (S-Es)` séquentiellement = Σ assets, et adapte les
    plages des en-têtes de section. Les numéros de plans et le reste sont
    strictement inchangés."""
    lines = (script or "").splitlines()
    plans = parse_plans(script)
    if not plans:
        return script

    cursor = 0.0
    for p in plans:
        p["_start"] = cursor
        p["_end"] = cursor + plan_required_duration(p)
        cursor = p["_end"]

    repl: dict[int, str] = {}
    for p in plans:
        repl[p["title_line"]] = _fmt_plan_title(
            lines[p["title_line"]], p["num"], p["_start"], p["_end"]
        )

    headers = parse_section_headers(script)
    header_lines = [h["line"] for h in headers]
    for hi, h in enumerate(headers):
        if not h["had_range"]:
            continue
        start_bound = h["line"]
        end_bound = header_lines[hi + 1] if hi + 1 < len(header_lines) else len(lines) + 1
        span = [p for p in plans if start_bound < p["title_line"] < end_bound]
        if not span:
            continue
        s = min(p["_start"] for p in span)
        e = max(p["_end"] for p in span)
        repl[h["line"]] = _fmt_section_header(lines[h["line"]], s, e)

    return "\n".join(repl.get(i, l) for i, l in enumerate(lines))


def repair_pacing_script(script: str, max_assets: int = 2) -> str | None:
    """Filet déterministe (zéro LLM): ramène chaque plan à max 2 vidéos (retire
    les excédentaires), ajoute une 2e vidéo (T2V≈4s) si une seule ne suffit
    plus pour la VO (plafonné), RACCOURCIT les VO qui dépassent même le budget
    2-assets (phrases entières d'abord, jamais de mot coupé) au lieu de déclarer
    forfait, puis retime tout le script. None seulement si impossible."""
    plans = parse_plans(script or "")
    if not plans:
        return None

    lines = (script or "").splitlines()
    drop: set[int] = set()
    additions: list[tuple[int, str]] = []
    vo_rewrites: dict[int, str] = {}

    for p in plans:
        # max 2 vidéos par plan : retire toutes les lignes Video: au-delà de 2
        if len(p["video_lines"]) > 2:
            for v in p["video_lines"][2:]:
                drop.add(v["line"])

        words = plan_vo_words(p)
        kept = p["video_lines"][:2]
        kept_dur = sum(asset_duration_of(p, v) for v in kept)
        budget = (2 * max(kept_dur, 1.0)) + 4
        n_assets = len(kept)
        base = kept[0]["text"] if kept else ""
        attempts = 0
        while words > budget and n_assets < max_assets:
            n_assets += 1
            attempts += 1
            variant = (
                f"{base} — alternate camera angle {attempts}" if base
                else f"Alternate camera angle {attempts}, dynamic cinematic action"
            )
            anchor = kept[-1]["line"] if kept else p["title_line"]
            additions.append((anchor, f"Video: {variant}"))
            budget = (2 * (kept_dur + attempts * T2V_SEC)) + 4
        if words > budget:
            # La VO dépasse même le budget 2-assets : la raccourcir au budget
            # (plus longue d'abord), au lieu de déclarer forfait.
            for v in sorted(p["vo_lines"],
                            key=lambda v: -_count_words(v["text"])):
                if words <= budget:
                    break
                share = max(budget - (words - _count_words(v["text"])), 1)
                new_text = _shorten_vo_to_budget(v["text"], share)
                if new_text != v["text"]:
                    m = _VO_RE.match(lines[v["line"]])
                    prefix = lines[v["line"]][:m.start(1)] if m else "VO: "
                    vo_rewrites[v["line"]] = prefix + new_text
                    words -= _count_words(v["text"]) - _count_words(new_text)
        if words > budget:
            return None

    if not additions and not drop and not vo_rewrites:
        # Aucun ajout/raccourcissement nécessaire, mais les horaires déclarés
        # peuvent quand même être incohérents avec la somme des assets (cas le
        # plus fréquent du run 28/09 : VO dans le budget, 1 asset, plage fausse
        # type `Plan 1 (0-8s)` pour 3s réelles). Le retiming seul suffit alors.
        retimed = rewrite_plan_timestamps(script)
        if retimed != script:
            return retimed
        return None

    # retire les vidéos excédentaires (indices de lignes) + applique les VO
    # raccourcies (aucun décalage d'indice : même nombre de lignes)
    new_lines = []
    _drop_iter = iter(sorted(drop))
    _next_drop = next(_drop_iter, None)
    for i, raw in enumerate(lines):
        if _next_drop == i:
            _next_drop = next(_drop_iter, None)
            continue
        new_lines.append(vo_rewrites.get(i, raw))

    # insère les ajouts (indices de lignes d'origine) après suppression
    add_map: dict[int, list[str]] = {}
    for anchor, text in additions:
        add_map.setdefault(anchor, []).append(text)

    # recalcule les indices d'insertion après les suppressions
    new_adds: dict[int, list[str]] = {}
    for old_anchor, texts in sorted(add_map.items(), reverse=True):
        cnt_deleted = sum(1 for d in drop if d <= old_anchor)
        anchor = old_anchor - cnt_deleted
        new_adds.setdefault(anchor, []).extend(texts)

    for anchor, texts in sorted(new_adds.items(), reverse=True):
        for t in reversed(texts):
            new_lines.insert(anchor + 1, t)

    retimed = "\n".join(new_lines)
    return rewrite_plan_timestamps(retimed)