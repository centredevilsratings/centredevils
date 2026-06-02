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
  IMAGO_API_BASE            default "https://api.imago-images.com/api"
  IMAGO_SEARCH_PATH         default "/search"
  IMAGO_IMAGE_URL_TEMPLATE  default "https://www.imago-images.com/bild/{db}/{id}/s.jpg"
                            ({db} is the 2-char code, {id} is the zero-padded pictureid)
  IMAGO_DEBUG               "1" to log raw responses at INFO (default: logs only on miss)

Schema confirmed via imago_probe.py:
  Auth headers: X-API-User + X-API-Key (NOT lowercase api-user/api-key, NOT Basic, NOT Bearer)
  Request:      POST /search with {"searchquery", "querystring", "size", "from": 0}
  Response:     [{"took": int, "total": int}, {"pictures": [{...}, ...]}]
  Per picture:  pictureid (int), db ("stock"/"sport"), caption, source, byline, ...
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Optional

import httpx

log = logging.getLogger("football-bot.imago")

API_USER = os.environ.get("IMAGO_API_USER", "")
API_KEY = os.environ.get("IMAGO_API_KEY", "")
API_BASE = os.environ.get("IMAGO_API_BASE", "https://api.imago-images.com/api").rstrip("/")
SEARCH_PATH = os.environ.get("IMAGO_SEARCH_PATH", "/search")
IMAGE_URL_TEMPLATE = os.environ.get(
    "IMAGO_IMAGE_URL_TEMPLATE",
    "https://www.imago-images.com/bild/{db}/{id}/s.jpg",
)
DEBUG = os.environ.get("IMAGO_DEBUG", "") == "1"

# Map the API's verbose db name to its 2-char URL slug.
_DB_MAP = {"stock": "st", "sport": "sp"}


def is_configured() -> bool:
    return bool(API_USER and API_KEY)


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


def _strip_math_bold(s: str) -> str:
    """Replace Mathematical Bold glyphs (the bold KEY fact + RECORD label)
    with spaces — they're noise for an image search."""
    return "".join(" " if 0x1D400 <= ord(c) <= 0x1D7FF else c for c in s)


def _deaccent(s: str) -> str:
    """ü→u, é→e, etc. so names survive the ASCII pass (Hütter → Hutter)."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def query_from_draft(draft: str) -> str:
    """Derive a clean photo search query (subject names) from a draft.

    Takes the first line and strips the 🚨 prefix, the label, the [@source]
    tag, the math-bold KEY fact, and emojis/flags — leaving the plain
    subject names that IMAGO should match on. Accented names are
    transliterated rather than dropped.
    """
    first = (draft or "").splitlines()[0] if draft else ""
    is_quote = "🎙" in first                           # quote format marker
    first = _PREFIX_RE.sub("", first)
    first = _strip_math_bold(first)                    # bold KEY + RECORD label → spaces
    first = _LABEL_RE.sub("", first)                   # plain-ascii labels
    first = _SOURCE_RE.sub("", first)
    first = _deaccent(first)                            # keep accented names
    first = _NON_ASCII_RE.sub(" ", first)              # drop emojis / flags
    if is_quote:
        # Quote subject is the speaker name before the first colon.
        first = first.split(":")[0]
    else:
        # A stripped bold RECORD label leaves an orphan leading colon.
        first = re.sub(r"^\s*:\s*", "", first)
    first = re.sub(r"\s+", " ", first).strip(" .,!?-:")
    return first[:120]


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
