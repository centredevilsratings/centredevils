"""
IMAGO Images API client — fetches a clean licensed press photo for a draft.

CentreGoals attaches the tweet's own media today, which is often a branded
"DONE DEAL" graphic or a video still. This module queries IMAGO's photo API
for a clean editorial photo of the subject (player / club / manager) and
returns a thumbnail URL to embed in the Discord draft.

The IMAGO API is Elasticsearch-backed. Because this was built without live
access to the API, the uncertain parts — search endpoint, request payload,
response shape, and the image-URL pattern — are ALL overridable via env vars
and the raw response is logged so the schema can be confirmed/corrected from
the first real run. See imago_probe.py for a standalone schema probe.

Env vars:
  IMAGO_API_USER            (required) — "imagoapi"
  IMAGO_API_KEY             (required) — the API key
  IMAGO_ENABLED             "1" to turn the integration on. Defaults to OFF
                            because imago-images.com is behind a BunnyCDN
                            JS-challenge shield that 403s every non-browser
                            fetcher (including Discord's image proxy) — see
                            the diagnosis in the commit history. Flip to "1"
                            once IMAGO confirms a working download endpoint.
  IMAGO_API_BASE            default "https://api.imago-images.com/api"
  IMAGO_SEARCH_PATH         default "/search"
  IMAGO_IMAGE_URL_TEMPLATE  default "https://www.imago-images.com/bild/{db}/{id}/w.jpg"
                            ({db} is the 2-char code, {id} is the zero-padded pictureid;
                             "w.jpg" is the web/direct JPG — "s.jpg" returns an HTML page)
  IMAGO_DEBUG               "1" to log raw responses at INFO (default: logs only on miss)

Schema confirmed via imago_probe.py:
  Auth headers: X-API-User + X-API-Key (NOT lowercase api-user/api-key, NOT Basic, NOT Bearer)
  Request:      POST /search with {"searchquery", "querystring", "size", "from": 0}
  Response:     [{"took": int, "total": int}, {"pictures": [{...}, ...]}]
  Per picture:  pictureid (int), db ("stock"/"sport"), caption, source, byline, ...
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from typing import Optional

import httpx

log = logging.getLogger("football-bot.imago")

API_USER = os.environ.get("IMAGO_API_USER", "")
API_KEY = os.environ.get("IMAGO_API_KEY", "")
ENABLED = os.environ.get("IMAGO_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
API_BASE = os.environ.get("IMAGO_API_BASE", "https://api.imago-images.com/api").rstrip("/")
SEARCH_PATH = os.environ.get("IMAGO_SEARCH_PATH", "/search")
IMAGE_URL_TEMPLATE = os.environ.get(
    "IMAGO_IMAGE_URL_TEMPLATE",
    "https://www.imago-images.com/bild/{db}/{id}/w.jpg",
)
DEBUG = os.environ.get("IMAGO_DEBUG", "") == "1"

# Map the API's verbose db name to its 2-char URL slug.
_DB_MAP = {"stock": "st", "sport": "sp"}


def is_configured() -> bool:
    """True only when the integration is BOTH credential-wired AND enabled.
    Returning False makes both call sites (X-stream consumer, article
    drafter) skip IMAGO and fall back to the tweet/article's own image."""
    return ENABLED and bool(API_USER and API_KEY)


def _headers() -> dict:
    return {
        "X-API-User": API_USER,
        "X-API-Key": API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ─── Query derivation ────────────────────────────────────────────────────────
_PREFIX_RE = re.compile(r"^[^A-Za-z]*?\|\s*")          # 🚨🚨| or 🚨🚨🎙️|
_LABEL_RE = re.compile(r"^(?:BREAKING|JUST IN|NEW|OFFICIAL|RECORD)\s*:\s*", re.I)
_SOURCE_RE = re.compile(r"\[@[^\]]+\]")
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")            # strips leftover emojis/flags

# Proper-noun matcher. First char: capital (incl. accented). Body: one or
# more lowercase / apostrophe / hyphen (handles "Mbappé", "N'Golo", "Hütter"),
# optionally followed by a second capital + lowercase run ("D'Ambrosio",
# "McDonald", "N'Golo"). Multiple Title-Case words joined by spaces are
# captured as one match ("Harry Kane", "Manchester United"). Lowercase
# particles like "van" / "de" are also allowed between Title-Case words
# ("Virgil van Dijk", "Luis de la Fuente").
_NAME_RE = re.compile(
    r"\b[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]+(?:[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]*)?"
    r"(?:\s+(?:van|von|de|del|della|di|da|le|la|el|al|bin|ibn|der|den|dos|do)"
    r"\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]+)*"
    r"(?:\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]+(?:[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]*)?)*"
)

# Football institutions — clubs, leagues, countries used as national teams,
# governing bodies. Names appearing in a draft that match these get
# de-prioritised so person names (players, coaches, managers) win.
# Lowercase-compared so case is irrelevant.

# Tokens whose presence anywhere in a name marks it as a club/league/body.
# (e.g. any "Manchester United", "Real Madrid", "Bayern X", "X League".)
_INSTITUTION_TOKENS = {
    # Generic club prefixes / suffixes
    "united", "city", "fc", "ac", "cf", "afc", "sc", "town", "wanderers",
    "albion", "athletic", "athletico", "rovers", "forest", "hotspur",
    "real", "atletico", "atletic", "bayern", "borussia", "olympique",
    "inter", "as", "rb", "eintracht", "bayer", "racing", "sporting",
    "vitoria", "saint",
    # League / governing body keywords
    "league", "liga", "bundesliga", "serie", "ligue", "eredivisie",
    "championship", "champions", "europa", "conference", "uefa", "fifa",
    "concacaf", "caf", "conmebol", "mls",
    # National team indicators
    "national",
    # Awards & trophies (Golden Shoe/Boot/Ball, Ballon d'Or, FIFA Best, World Cup)
    "golden", "ballon", "boot", "shoe", "ball", "cup", "trophy", "award",
    "best",
}

# Whole-name (lowercased) matches: single-word clubs and country/national-team
# names that appear in football news.
_INSTITUTION_NAMES = {
    # Premier League
    "barcelona", "liverpool", "arsenal", "chelsea", "tottenham", "newcastle",
    "brighton", "brentford", "burnley", "everton", "fulham", "leeds",
    "sheffield", "southampton", "watford", "wolves", "bournemouth",
    "leicester", "sunderland", "preston", "luton", "ipswich",
    # La Liga
    "sevilla", "valencia", "villarreal", "betis", "getafe", "osasuna",
    "espanyol", "mallorca", "celta", "elche", "levante", "granada", "cadiz",
    "alaves", "girona", "rayo", "almeria",
    # Serie A
    "juventus", "roma", "lazio", "napoli", "atalanta", "fiorentina", "torino",
    "sassuolo", "bologna", "udinese", "empoli", "sampdoria", "genoa",
    "verona", "cagliari", "spezia", "salernitana", "lecce", "cremonese",
    "monza", "milan",
    # Bundesliga
    "dortmund", "leipzig", "leverkusen", "frankfurt", "stuttgart", "wolfsburg",
    "hoffenheim", "schalke", "werder", "hertha", "augsburg", "freiburg",
    "mainz", "koln", "monchengladbach", "mönchengladbach",
    # Ligue 1
    "psg", "marseille", "lyon", "monaco", "lille", "rennes", "nice",
    "strasbourg", "nantes", "bordeaux", "toulouse", "reims", "brest", "lens",
    "montpellier", "angers", "lorient", "auxerre", "metz",
    # Eredivisie
    "ajax", "psv", "feyenoord", "twente", "utrecht", "alkmaar", "heerenveen",
    "vitesse", "groningen", "az",
    # Primeira Liga
    "porto", "benfica", "braga", "boavista",
    # Other European
    "fenerbahce", "galatasaray", "besiktas", "trabzonspor",
    "celtic", "rangers", "hearts", "hibernian", "aberdeen",
    "shakhtar", "dynamo", "zenit", "spartak", "cska",
    # Brazilian / South American
    "fluminense", "flamengo", "palmeiras", "santos", "corinthians",
    "river", "boca", "independiente",
    # Saudi / Asian
    "alnassr", "alhilal", "alittihad", "alahly", "alshabab",
    # National teams / countries
    "england", "france", "germany", "spain", "italy", "portugal",
    "netherlands", "belgium", "croatia", "morocco", "argentina", "brazil",
    "uruguay", "colombia", "chile", "mexico", "usa", "japan", "korea",
    "australia", "qatar", "iran", "iraq", "denmark", "sweden", "norway",
    "poland", "ukraine", "wales", "scotland", "ireland", "switzerland",
    "austria", "turkey", "ghana", "senegal", "nigeria", "egypt", "cameroon",
    "tunisia", "algeria", "ecuador", "peru", "paraguay", "serbia", "russia",
    "greece", "bulgaria", "romania", "hungary", "czech", "slovakia",
    "europe", "americas",
}


def _strip_math_bold(s: str) -> str:
    """Replace Mathematical Bold glyphs (the bold KEY fact + RECORD label)
    with spaces — they're noise for an image search."""
    return "".join(" " if 0x1D400 <= ord(c) <= 0x1D7FF else c for c in s)


def _deaccent(s: str) -> str:
    """ü→u, é→e, etc. so names survive the ASCII pass (Hütter → Hutter)."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _is_institution(name: str) -> bool:
    """True when a Title-Case name looks like a club, league, governing body,
    or national-team country rather than a person."""
    tokens = name.lower().split()
    if any(t in _INSTITUTION_TOKENS for t in tokens):
        return True
    return all(t in _INSTITUTION_NAMES for t in tokens)


def _normalise(name: str) -> str:
    """Deaccent + ASCII-only for the API query."""
    return _NON_ASCII_RE.sub("", _deaccent(name)).strip()[:80]


def subjects_from_draft(draft: str, max_subjects: int = 2) -> list:
    """Extract up to `max_subjects` photo-search subjects from a draft.

    Persons (players, coaches, managers) always win over clubs / leagues /
    countries — IMAGO returns much sharper photos for "Harry Kane" than for
    "Tottenham". When the draft mentions multiple persons ("Yan Diomande on
    Michael Olise", "Pep Guardiola signs Erling Haaland"), returns both so
    the operator can attach both photos.

    Falls back to a club/league name only when no person names appear, so
    drafts about "Premier League TV deal" still get a relevant image.
    """
    first = (draft or "").splitlines()[0] if draft else ""
    is_quote = "🎙" in first
    first = _PREFIX_RE.sub("", first)
    first = _strip_math_bold(first)
    first = _LABEL_RE.sub("", first)
    first = _SOURCE_RE.sub("", first)
    first = re.sub(r"^\s*:\s*", "", first)
    first = re.sub(r"\s+", " ", first).strip(" .,!?-:")

    # For quote drafts, restrict to the speaker line (before the colon) so
    # the search is about the speaker and any person they're talking about,
    # not the contents of the quote itself.
    if is_quote and ":" in first:
        first = first.split(":")[0].strip()

    names = _NAME_RE.findall(first)
    persons, institutions = [], []
    for n in names:
        (institutions if _is_institution(n) else persons).append(n)

    # De-dup while preserving order (case-insensitive).
    def _dedup(seq):
        seen, out = set(), []
        for x in seq:
            k = x.lower()
            if k not in seen:
                seen.add(k)
                out.append(x)
        return out

    persons = _dedup(persons)
    if persons:
        chosen = persons[:max_subjects]
    elif institutions:
        chosen = _dedup(institutions)[:1]
    else:
        chosen = [first] if first else []

    return [_normalise(s) for s in chosen if _normalise(s)]


# Back-compat shim for callers still using the single-string API.
def query_from_draft(draft: str) -> str:
    subs = subjects_from_draft(draft, max_subjects=1)
    return subs[0] if subs else ""


# ─── Search ──────────────────────────────────────────────────────────────────
def _build_payload(query: str, limit: int) -> dict:
    """Best-guess IMAGO/ES search payload. Overridable in spirit via the
    response being logged so we can correct quickly."""
    return {
        "searchquery": query,
        "querystring": query,
        "size": limit,
        "from": 0,
    }


def _extract_hits(data) -> list:
    """Pull the list of picture objects out of IMAGO's response.

    Confirmed shape: a 2-element list — [{"took":..,"total":..}, {"pictures":[...]}].
    Falls back to a few other shapes in case the API evolves.
    """
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and isinstance(entry.get("pictures"), list):
                return entry["pictures"]
        return []
    if isinstance(data, dict):
        for key in ("pictures", "hits", "results", "data", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


def _hit_to_url(hit: dict) -> Optional[str]:
    """Build a thumbnail URL from a pictures[] entry.

    Schema: {"pictureid": int, "db": "stock"|"sport", ...}
    URL pattern: https://www.imago-images.de/bild/{db_slug}/{id_zero_padded_10}/s.jpg
    """
    if not isinstance(hit, dict):
        return None
    pid = hit.get("pictureid") or hit.get("bildnummer") or hit.get("id")
    if pid is None:
        return None
    db_raw = str(hit.get("db") or "stock").lower()
    db = _DB_MAP.get(db_raw, db_raw[:2])
    pid_str = str(pid).zfill(10)
    try:
        return IMAGE_URL_TEMPLATE.format(db=db, id=pid_str)
    except Exception:
        return None


def _is_portrait(hit: dict) -> bool:
    """True when the picture is taller than wide. height/width come back as
    strings in the API response."""
    try:
        h = int(hit.get("height") or 0)
        w = int(hit.get("width") or 0)
    except (TypeError, ValueError):
        return False
    return h > 0 and w > 0 and h > w


async def search_photo(client: httpx.AsyncClient, query: str,
                       limit: int = 100) -> Optional[str]:
    """Return a thumbnail URL for the best matching IMAGO portrait photo,
    or None.

    Filters to portrait orientation (height > width) per operator preference
    — landscape photos crop awkwardly in Discord embeds. Searches with a
    larger limit so we still have candidates after filtering.

    Never raises — any failure logs and returns None so the draft pipeline
    falls back to posting with no photo.
    """
    if not is_configured():
        log.debug("IMAGO not configured; skipping photo lookup")
        return None
    query = (query or "").strip()
    if not query:
        return None

    url = f"{API_BASE}{SEARCH_PATH}"
    try:
        resp = await client.post(url, json=_build_payload(query, limit),
                                 headers=_headers(), timeout=15)
    except Exception as e:
        log.warning(f"IMAGO request failed for {query!r}: {e}")
        return None

    if resp.status_code != 200:
        log.warning(f"IMAGO HTTP {resp.status_code} for {query!r}: "
                    f"{resp.text[:300]}")
        return None

    try:
        data = resp.json()
    except Exception:
        log.warning(f"IMAGO non-JSON response for {query!r}: {resp.text[:300]}")
        return None

    hits = _extract_hits(data)
    if not hits:
        log.warning(f"IMAGO no hits parsed for {query!r}; raw="
                    f"{str(data)[:400]}")
        return None

    portraits = [h for h in hits if _is_portrait(h)]
    if DEBUG:
        log.info(f"IMAGO {query!r}: {len(hits)} hits, "
                 f"{len(portraits)} portrait")

    if not portraits:
        log.info(f"IMAGO no portrait hits for {query!r} "
                 f"({len(hits)} landscape/square skipped)")
        return None

    for hit in portraits:
        photo_url = _hit_to_url(hit)
        if photo_url:
            log.info(f"IMAGO photo for {query!r}: {photo_url}")
            return photo_url

    log.warning(f"IMAGO portrait hits found but no URL built for {query!r}; "
                f"hit[0]={str(portraits[0])[:400]}")
    return None


async def search_photos(client: httpx.AsyncClient, subjects: list,
                        limit: int = 100) -> list:
    """Run search_photo() for each subject in parallel; return the URLs
    (excluding any that returned None / failed)."""
    if not subjects:
        return []
    results = await asyncio.gather(
        *(search_photo(client, s, limit) for s in subjects),
        return_exceptions=False,
    )
    return [u for u in results if u]
