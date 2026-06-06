"""
Subject extraction utility.

Originally an IMAGO Images API client for fetching licensed press photos
to embed in Discord drafts. Per operator decision the whole image feature
has been scrapped — drafts post as text + source link only.

What remains here is the proper-noun / person-name extractor
(`subjects_from_draft`), kept because the soft-key dedup in
tweet_drafter.py still uses it to derive the subject portion of the
"<subject>|<action>" content fingerprint. Nothing in this module makes
network calls or reads IMAGO env vars anymore.
"""

from __future__ import annotations

import re
import unicodedata


# ─── Subject extraction ──────────────────────────────────────────────────────
_PREFIX_RE = re.compile(r"^[^A-Za-z]*?\|\s*")          # 🚨🚨| or 🚨🚨🎙️|
_LABEL_RE = re.compile(r"^(?:BREAKING|JUST IN|NEW|OFFICIAL|RECORD)\s*:\s*", re.I)
_SOURCE_RE = re.compile(r"\[@[^\]]+\]")
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")

# Proper-noun matcher. First char: capital (incl. accented). Body: one or
# more lowercase / apostrophe / hyphen (handles "Mbappé", "N'Golo", "Hütter"),
# optionally followed by a second capital + lowercase run ("D'Ambrosio",
# "McDonald"). Multiple Title-Case words joined by spaces are captured as
# one match ("Harry Kane", "Manchester United"). Lowercase particles like
# "van" / "de" are also allowed between Title-Case words ("Virgil van Dijk").
_NAME_RE = re.compile(
    r"\b[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]+(?:[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]*)?"
    r"(?:\s+(?:van|von|de|del|della|di|da|le|la|el|al|bin|ibn|der|den|dos|do)"
    r"\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]+)*"
    r"(?:\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]+(?:[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'\-]*)?)*"
)

# Football institutions — clubs, leagues, countries used as national teams,
# governing bodies. Names appearing in a draft that match these get
# de-prioritised so person names (players, coaches, managers) win.
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
    """Replace Mathematical Bold glyphs with spaces — noise for matching."""
    return "".join(" " if 0x1D400 <= ord(c) <= 0x1D7FF else c for c in s)


def _deaccent(s: str) -> str:
    """ü→u, é→e, etc. so names survive the ASCII pass."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _is_institution(name: str) -> bool:
    tokens = name.lower().split()
    if any(t in _INSTITUTION_TOKENS for t in tokens):
        return True
    return all(t in _INSTITUTION_NAMES for t in tokens)


def _normalise(name: str) -> str:
    return _NON_ASCII_RE.sub("", _deaccent(name)).strip()[:80]


def subjects_from_draft(draft: str, max_subjects: int = 2) -> list:
    """Extract up to `max_subjects` person/proper-noun subjects from a draft.

    Persons (players, coaches, managers) always win over clubs / leagues /
    countries. Falls back to a club/league name only when no person names
    appear. Used by the soft-key dedup to derive the subject portion of the
    "<subject>|<action>" content fingerprint.
    """
    first = (draft or "").splitlines()[0] if draft else ""
    is_quote = "🎙" in first
    first = _PREFIX_RE.sub("", first)
    first = _strip_math_bold(first)
    first = _LABEL_RE.sub("", first)
    first = _SOURCE_RE.sub("", first)
    first = re.sub(r"^\s*:\s*", "", first)
    first = re.sub(r"\s+", " ", first).strip(" .,!?-:")

    if is_quote and ":" in first:
        first = first.split(":")[0].strip()

    names = _NAME_RE.findall(first)
    persons, institutions = [], []
    for n in names:
        (institutions if _is_institution(n) else persons).append(n)

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
