"""
Football Ops Alert Bot
Monitors football news, translates to English, posts to Discord by league.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus, urlparse

import anthropic
import feedparser
import httpx
from bs4 import BeautifulSoup
from langdetect import detect

import tweet_drafter
import x_stream

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("football-bot")

# ─── Config ──────────────────────────────────────────────────────────────────
POLL_INTERVAL = 75  # seconds
CLUSTER_WINDOW = 6 * 3600  # 6 hours in seconds
DB_PATH = os.environ.get("DB_PATH", "football_ops.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

WEBHOOKS = {
    "premier_league": os.environ.get("PREMIER_LEAGUE_WEBHOOK", ""),
    "la_liga": os.environ.get("LA_LIGA_WEBHOOK", ""),
    "serie_a": os.environ.get("SERIE_A_WEBHOOK", ""),
    "bundesliga": os.environ.get("BUNDESLIGA_WEBHOOK", ""),
    "ligue_1": os.environ.get("LIGUE_1_WEBHOOK", ""),
    "other": os.environ.get("OTHER_LEAGUES_WEBHOOK", ""),
    "tweet_drafts": os.environ.get("TWEET_DRAFTS_WEBHOOK", ""),
    "man_utd": os.environ.get("MAN_UTD_WEBHOOK", ""),
}

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")

# Per-club webhook routes. Set the env var on Render (e.g. MAN_UTD_WEBHOOK)
# to enable a club-specific channel — articles matching that club go there
# instead of the league channel. Falls back to the league channel if the
# club's env var isn't set.
CLUB_ROUTES = {
    "man_utd": {
        "aliases": ("Man Utd", "Manchester United", "Man United", "MUFC", "Red Devils"),
        "label": "Manchester United",
        "emoji": "🔴",
        "league": "premier_league",
        "cap": 12,
    },
}

# Per-webhook hourly post caps (rolling 1h window).
# tweet_drafts is intentionally uncapped — every actionable item should land
# in the drafts channel immediately.
HOURLY_CAPS = {
    "premier_league": 8,
    "la_liga": 6,
    "serie_a": 6,
    "bundesliga": 5,
    "ligue_1": 5,
    "other": 6,
}

LEAGUE_CLUBS = {
    "premier_league": [
        "Man Utd", "Manchester United",
        "Man City", "Manchester City",
        "Liverpool",
        "Chelsea",
        "Arsenal",
        "Tottenham", "Tottenham Hotspur", "Spurs",
        "Newcastle", "Newcastle United",
        "Aston Villa",
    ],
    "la_liga": [
        "Real Madrid",
        "Barcelona", "FC Barcelona",
        "Atletico Madrid", "Atlético Madrid",
    ],
    "serie_a": [
        "Como", "Como 1907",
        "Juventus",
        "Inter Milan", "Internazionale",
        "AC Milan", "Milan",
        "Napoli",
    ],
    "bundesliga": [
        "Bayern Munich", "Bayern",
        "Dortmund", "Borussia Dortmund", "BVB",
        "Bayer Leverkusen", "Leverkusen",
    ],
    "ligue_1": [
        "PSG", "Paris Saint-Germain", "Paris SG",
        "Marseille", "Olympique de Marseille", "OM",
    ],
    "other": [
        "Ajax",
        "FC Porto", "Porto",
        "Sporting", "Sporting CP", "Sporting Lisbon",
        "Galatasaray",
        "Besiktas", "Beşiktaş",
        "Fenerbahce", "Fenerbahçe",
        "Inter Miami",
        "Al-Nassr", "Al Nassr",
        "Al Hilal", "Al-Hilal",
    ],
}

# Primary search queries (English + local language variants)
SEARCH_QUERIES = [
    # Premier League
    "Manchester United transfer news",
    "Manchester City news",
    "Liverpool FC news",
    "Chelsea FC transfer",
    "Arsenal FC news",
    "Tottenham Hotspur news",
    "Newcastle United news",
    "Aston Villa news",
    # La Liga
    "Real Madrid noticias",
    "FC Barcelona noticias",
    "Atletico Madrid noticias",
    # Serie A
    "Como 1907 notizie",
    "Juventus notizie",
    "Inter Milan notizie",
    "AC Milan notizie",
    "Napoli notizie",
    # Bundesliga
    "Bayern München Neuigkeiten",
    "Borussia Dortmund Neuigkeiten",
    "Bayer Leverkusen Neuigkeiten",
    # Ligue 1
    "PSG actualités",
    "Olympique Marseille actualités",
    # Other
    "Ajax nieuws",
    "FC Porto noticias",
    "Sporting CP noticias",
    "Galatasaray haber",
    "Besiktas haber",
    "Fenerbahce haber",
    "Inter Miami news",
    "Al-Nassr news",
    "Al Hilal news",
]

# Direct RSS feeds from major football outlets — supplement to Google News
# search. Pruned to known-working endpoints; dead/blocked feeds removed
# (Goal.com, Football365, AS, Calciomercato, Daily Mail, Get French,
# Liverpool Echo, Bavarian Football Works, Football Italia, Tuttomercatoweb,
# Football España, O Jogo, SofaScore, Squawka).
RSS_SOURCES: list[dict] = [
    {"name": "BBC Sport Football", "url": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
    {"name": "Guardian Football", "url": "https://www.theguardian.com/football/rss"},
    {"name": "Sky Sports Football", "url": "https://www.skysports.com/rss/12040"},
    {"name": "Independent Football", "url": "https://www.independent.co.uk/sport/football/rss"},
    {"name": "ESPN FC", "url": "https://www.espn.com/espn/rss/soccer/news"},
    {"name": "Telegraph Football", "url": "https://www.telegraph.co.uk/football/rss.xml"},
    {"name": "Manchester Evening News", "url": "https://www.manchestereveningnews.co.uk/sport/football/?service=rss"},
    {"name": "L'Équipe", "url": "https://dwh.lequipe.fr/api/edito/rss?path=/Football/"},
    {"name": "RMC Sport", "url": "https://rmcsport.bfmtv.com/rss/football/"},
    {"name": "Marca (EN)", "url": "https://e00-marca.uecdn.es/rss/en/football/barcelona.xml"},
    {"name": "Get Spanish Football News", "url": "https://getfootballnewsspain.com/feed/"},
    {"name": "Get Italian Football News", "url": "https://www.getfootballnewsitaly.com/feed/"},
    {"name": "Get German Football News", "url": "https://www.getfootballnewsgermany.com/feed/"},
]


LEAGUE_COLORS = {
    "premier_league": 0x3D195B,   # Purple
    "la_liga": 0xFF4500,           # Orange-red
    "serie_a": 0x0066CC,           # Blue
    "bundesliga": 0xFF0000,        # Red
    "ligue_1": 0x003399,           # Dark blue
    "other": 0x2ECC71,             # Green
}

LEAGUE_EMOJIS = {
    "premier_league": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "la_liga": "🇪🇸",
    "serie_a": "🇮🇹",
    "bundesliga": "🇩🇪",
    "ligue_1": "🇫🇷",
    "other": "🌍",
}

LEAGUE_LABELS = {
    "premier_league": "Premier League",
    "la_liga": "La Liga",
    "serie_a": "Serie A",
    "bundesliga": "Bundesliga",
    "ligue_1": "Ligue 1",
    "other": "Other Leagues",
}

URGENCY_COLORS = {
    1: 0x808080,  # Grey - low
    2: 0x3498DB,  # Blue - medium-low
    3: 0xF39C12,  # Orange - medium
    4: 0xE74C3C,  # Red - high
    5: 0xFF0000,  # Bright red - breaking
}


# Plug each configured club route into the existing label/emoji/cap maps
# so the rest of the code (embed builder, hourly caps) treats them like
# any other route.
for _route_key, _cfg in CLUB_ROUTES.items():
    LEAGUE_LABELS[_route_key] = _cfg["label"]
    LEAGUE_EMOJIS[_route_key] = _cfg["emoji"]
    HOURLY_CAPS[_route_key] = _cfg["cap"]


def route_for(league: str, club: str) -> str:
    """If the matched club has its own webhook configured, route there.
    Otherwise fall back to the league route."""
    if not club:
        return league
    club_lower = club.lower()
    for route_key, cfg in CLUB_ROUTES.items():
        if not WEBHOOKS.get(route_key):
            continue
        if any(a.lower() == club_lower for a in cfg["aliases"]):
            return route_key
    return league


# ─── Database ────────────────────────────────────────────────────────────────
def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            url TEXT UNIQUE,
            title_original TEXT,
            title_en TEXT,
            summary_en TEXT,
            source TEXT,
            published_at INTEGER,
            fetched_at INTEGER,
            language TEXT,
            league TEXT,
            club TEXT,
            urgency INTEGER,
            uniqueness INTEGER,
            cluster_id TEXT,
            posted INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clusters (
            id TEXT PRIMARY KEY,
            topic TEXT,
            first_seen INTEGER,
            last_updated INTEGER,
            article_count INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS post_log (
            webhook_key TEXT,
            posted_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS draft_log (
            story_id TEXT PRIMARY KEY,
            posted_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fetched ON articles(fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster ON articles(cluster_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_league ON articles(league)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_post_log ON post_log(webhook_key, posted_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_draft_log ON draft_log(posted_at)")

    _ensure_columns(conn, "articles", {
        "title_original": "TEXT",
        "title_en": "TEXT",
        "summary_en": "TEXT",
        "source": "TEXT",
        "published_at": "INTEGER",
        "fetched_at": "INTEGER",
        "language": "TEXT",
        "league": "TEXT",
        "club": "TEXT",
        "urgency": "INTEGER",
        "uniqueness": "INTEGER",
        "cluster_id": "TEXT",
        "posted": "INTEGER DEFAULT 0",
    })

    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str,
                    expected: dict[str, str]) -> None:
    """Add any missing columns to an existing table (forward migration)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, ddl in expected.items():
        if col not in existing:
            log.info(f"Schema migration: ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


# ─── Hourly caps ─────────────────────────────────────────────────────────────
def hourly_cap_available(conn: sqlite3.Connection, key: str) -> bool:
    cap = HOURLY_CAPS.get(key)
    if cap is None:
        return True
    cutoff = int(time.time()) - 3600
    count = conn.execute(
        "SELECT COUNT(*) FROM post_log WHERE webhook_key=? AND posted_at>?",
        (key, cutoff),
    ).fetchone()[0]
    return count < cap


def hourly_cap_record(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "INSERT INTO post_log(webhook_key, posted_at) VALUES (?, ?)",
        (key, int(time.time())),
    )
    # Prune entries older than 2h to keep the table tiny.
    conn.execute("DELETE FROM post_log WHERE posted_at < ?",
                 (int(time.time()) - 7200,))
    conn.commit()


# ─── Draft dedup (story_id-based, 12h window) ────────────────────────────────
DRAFT_DEDUP_WINDOW = 12 * 3600


def draft_already_posted(conn: sqlite3.Connection, story_id: str) -> bool:
    if not story_id:
        return False
    cutoff = int(time.time()) - DRAFT_DEDUP_WINDOW
    row = conn.execute(
        "SELECT 1 FROM draft_log WHERE story_id=? AND posted_at>? LIMIT 1",
        (story_id, cutoff),
    ).fetchone()
    return row is not None


def record_draft_posted(conn: sqlite3.Connection, story_id: str) -> None:
    if not story_id:
        return
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO draft_log(story_id, posted_at) VALUES (?, ?)",
        (story_id, now),
    )
    # Prune entries older than 48h.
    conn.execute("DELETE FROM draft_log WHERE posted_at < ?",
                 (now - 48 * 3600,))
    conn.commit()


# ─── Feed Fetching ────────────────────────────────────────────────────────────
def build_rss_url(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-GB&gl=GB&ceid=GB:en"


async def fetch_feed(client: httpx.AsyncClient, query: str) -> list[dict]:
    url = build_rss_url(query)
    try:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        items = []
        for entry in feed.entries[:10]:  # top 10 per query
            item = {
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "source": entry.get("source", {}).get("title", "Unknown"),
                "published": entry.get("published_parsed"),
            }
            if item["url"] and item["title"]:
                items.append(item)
        return items
    except Exception as e:
        log.warning(f"Feed fetch failed for '{query}': {type(e).__name__}: {e}")
        return []


async def fetch_rss(client: httpx.AsyncClient, source: dict) -> list[dict]:
    """Fetch a direct RSS feed (non-Google-News). Returns parsed items."""
    try:
        resp = await client.get(source["url"], timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        items = []
        for entry in feed.entries[:15]:
            url = entry.get("link", "")
            title = entry.get("title", "")
            if not url or not title:
                continue
            items.append({
                "title": title,
                "url": url,
                "source": source["name"],
                "published": entry.get("published_parsed"),
            })
        return items
    except Exception as e:
        log.warning(f"RSS fetch failed for {source['name']}: {type(e).__name__}: {e}")
        return []


async def extract_article_metadata(client: httpx.AsyncClient, url: str) -> dict:
    """Extract main text + og:image from an article URL."""
    try:
        resp = await client.get(url, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Preview image: og:image → twitter:image → first article <img>
        image_url = None
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            image_url = og["content"]
        if not image_url:
            tw = soup.find("meta", attrs={"name": "twitter:image"}) \
                or soup.find("meta", property="twitter:image")
            if tw and tw.get("content"):
                image_url = tw["content"]

        # Strip noise for text extraction.
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "ad"]):
            tag.decompose()

        article = soup.find("article")
        if article:
            text = article.get_text(separator=" ", strip=True)
        else:
            paragraphs = soup.find_all("p")
            text = " ".join(p.get_text(strip=True) for p in paragraphs)

        return {"text": text[:1500].strip(), "image_url": image_url}
    except Exception as e:
        log.debug(f"Article extraction failed for {url}: {e}")
        return {"text": "", "image_url": None}


async def extract_article_text(client: httpx.AsyncClient, url: str) -> str:
    """Backwards-compatible wrapper around extract_article_metadata."""
    meta = await extract_article_metadata(client, url)
    return meta["text"]


# ─── Article ID & Dedup ───────────────────────────────────────────────────────
def article_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def is_duplicate(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
    return row is not None


# ─── Cheap title pre-filter ──────────────────────────────────────────────────
_OPS_KEYWORDS = (
    "transfer", "sign", "signed", "signing", "deal", "agree", "agreed",
    "agreement", "move", "loan", "bid", "offer", "fee", "contract",
    "extend", "extension", "renew", "renewal", "release", "released",
    "free agent",
    "sack", "sacked", "fire", "fired", "dismiss", "dismissed", "axe",
    "resign", "step down", "exit", "leave", "leaves", "leaving", "depart",
    "return", "returns", "back to",
    "retire", "retires", "retiring", "retirement",
    "appoint", "appointed", "hire", "hired", "named", "new manager",
    "new head coach", "new boss", "new coach",
    "injury", "injured", "knock", "surgery", "out for", "ruled out",
    "miss", "misses", "missed", "fit", "unfit", "doubt", "recover",
    "recall", "recalled", "drop", "dropped",
    "ban", "banned", "suspension", "suspended",
    "ownership", "takeover", "owner", "buy", "bought", "sale",
    "captain", "vice-captain",
    "here we go", "done deal", "breaking", "just in", "exclusive",
)


def _looks_operational(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _OPS_KEYWORDS)


# Allowlist of trusted publications for the news-tweets draft pipeline.
# Anything outside this set still gets posted to the league channel as an
# embed (where the source is visible) but is NOT auto-drafted, because
# low-tier aggregators (nowarsenal, sportbible, fan blogs) routinely
# recycle old news with breaking-style headlines.
_TRUSTED_DRAFT_SOURCES = (
    "bbc", "guardian", "sky sports", "sky sport", "independent",
    "espn", "telegraph", "athletic", "times", "reuters", "ap news",
    "associated press", "afp",
    "fabrizio romano", "ornstein", "di marzio", "schira", "plettenberg",
    "l'équipe", "lequipe", "rmc", "le parisien", "france football",
    "marca", "as.com", "diario as", "mundo deportivo", "sport.es",
    "relevo",
    "gazzetta", "tuttosport", "corriere dello sport", "calciomercato",
    "football italia", "tuttomercatoweb",
    "bild", "kicker", "sport bild",
    "manchester evening news", "liverpool echo",
    "getfootballnews", "get french football news",
    "get spanish football news", "get italian football news",
    "get german football news",
    "goal.com",
)


def _is_trusted_for_drafts(source: str) -> bool:
    s = (source or "").lower()
    return any(kw in s for kw in _TRUSTED_DRAFT_SOURCES)


# ─── League Detection ─────────────────────────────────────────────────────────
def detect_league(text: str) -> tuple[str, str]:
    """Return (league_key, matched_club)."""
    text_lower = text.lower()
    for league, clubs in LEAGUE_CLUBS.items():
        for club in clubs:
            if club.lower() in text_lower:
                return league, club
    return "other", ""


# ─── Clustering ───────────────────────────────────────────────────────────────
def find_or_create_cluster(
    conn: sqlite3.Connection, title_en: str, league: str, now: int
) -> str:
    """Simple keyword-based clustering within 6-hour window."""
    # Get key words from title
    stop = {"the", "a", "an", "in", "of", "to", "is", "at", "on", "for", "and", "or"}
    words = set(w.lower() for w in re.findall(r"\w+", title_en) if len(w) > 3 and w.lower() not in stop)

    cutoff = now - CLUSTER_WINDOW
    candidates = conn.execute(
        """SELECT id, topic FROM clusters WHERE last_updated > ?""",
        (cutoff,),
    ).fetchall()

    best_cluster = None
    best_score = 0
    for cid, topic in candidates:
        topic_words = set(w.lower() for w in re.findall(r"\w+", topic) if len(w) > 3)
        overlap = len(words & topic_words)
        if overlap >= 2 and overlap > best_score:
            best_score = overlap
            best_cluster = cid

    if best_cluster:
        conn.execute(
            "UPDATE clusters SET last_updated=?, article_count=article_count+1 WHERE id=?",
            (now, best_cluster),
        )
        conn.commit()
        return best_cluster
    else:
        cid = hashlib.sha256(f"{title_en}{now}".encode()).hexdigest()[:12]
        conn.execute(
            "INSERT INTO clusters(id, topic, first_seen, last_updated) VALUES(?,?,?,?)",
            (cid, title_en[:100], now, now),
        )
        conn.commit()
        return cid


async def _draft_article_to_webhook(http: httpx.AsyncClient,
                                    conn: sqlite3.Connection,
                                    webhook_url: str,
                                    title: str, summary: str,
                                    source: str, url: str,
                                    image_url: Optional[str] = None) -> None:
    """Fire-and-forget: draft a CentreGoals tweet from an article and post it."""
    try:
        result = await asyncio.to_thread(
            tweet_drafter.draft_article, claude_client, title, summary, source,
        )
        if not result:
            return
        draft, story_id = result
        if draft_already_posted(conn, story_id):
            log.info(f"DUP draft skipped ({story_id}): {title[:60]}")
            return
        ok = await tweet_drafter.post_draft(
            http, webhook_url, draft, url, image_url=image_url,
        )
        if ok:
            record_draft_posted(conn, story_id)
            log.info(f"Drafted (article, {story_id}): {draft[:80]}")
    except Exception as e:
        log.error(f"Article drafter failed: {e}", exc_info=True)


def cluster_already_posted(conn: sqlite3.Connection, cluster_id: str) -> bool:
    """True if any article in this cluster was already posted."""
    row = conn.execute(
        "SELECT id FROM articles WHERE cluster_id=? AND posted=1 LIMIT 1",
        (cluster_id,),
    ).fetchone()
    return row is not None


# ─── Anthropic Processing ─────────────────────────────────────────────────────
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a football operations intelligence analyst.
Given a football news article (title + body text), return ONLY valid JSON with this exact schema:
{
  "title_en": "English headline, max 120 chars",
  "summary_en": "2-3 sentence English summary focused on operational impact (transfers, injuries, suspensions, manager news, financial deals)",
  "urgency": <integer 1-5 where 5=breaking/transfer confirmed, 4=strong rumour/injury, 3=developing story, 2=background, 1=filler>,
  "uniqueness": <integer 1-5 where 5=exclusive scoop, 1=widely reported>,
  "tags": ["tag1", "tag2"]
}
Do NOT wrap in markdown. Output raw JSON only."""


def process_with_claude(title: str, body: str, source_lang: str) -> Optional[dict]:
    prompt = f"""Title: {title}
Source language: {source_lang}
Body: {body[:1200]}

Analyse and return JSON."""
    try:
        msg = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        log.warning(f"Claude processing failed: {e}")
        return None


# ─── Discord Posting ──────────────────────────────────────────────────────────
async def post_discord_embed(
    client: httpx.AsyncClient,
    webhook_url: str,
    article: dict,
    league: str,
    cluster_size: int = 1,
) -> bool:
    if not webhook_url:
        log.warning(f"No webhook configured for {league}")
        return False

    urgency = article.get("urgency", 2)
    color = URGENCY_COLORS.get(urgency, URGENCY_COLORS[2])
    emoji = LEAGUE_EMOJIS.get(league, "⚽")
    label = LEAGUE_LABELS.get(league, league)

    urgency_labels = {1: "📰 Low", 2: "📋 Medium", 3: "🔔 Notable", 4: "🚨 High", 5: "🔴 BREAKING"}
    urgency_text = urgency_labels.get(urgency, "📋 Medium")

    tags = article.get("tags", [])
    tags_str = " ".join(f"`{t}`" for t in tags[:5]) if tags else ""

    footer_parts = [f"Source: {article.get('source', 'Unknown')}"]
    if cluster_size > 1:
        footer_parts.append(f"📦 {cluster_size} related stories")
    footer_parts.append(f"Lang: {article.get('language', '?').upper()}")

    embed = {
        "title": article["title_en"][:256],
        "url": article["url"],
        "description": article["summary_en"][:2048],
        "color": color,
        "fields": [
            {"name": "Urgency", "value": urgency_text, "inline": True},
            {"name": "League", "value": f"{emoji} {label}", "inline": True},
        ],
        "footer": {"text": " | ".join(footer_parts)},
        "timestamp": datetime.utcnow().isoformat(),
    }

    if article.get("club"):
        embed["fields"].append({"name": "Club", "value": article["club"], "inline": True})

    if tags_str:
        embed["fields"].append({"name": "Tags", "value": tags_str, "inline": False})

    payload = {"embeds": [embed]}
    if urgency == 5:
        payload["content"] = "@here 🔴 **BREAKING FOOTBALL NEWS**"

    try:
        resp = await client.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info(f"Posted to {league}: {article['title_en'][:60]}")
            return True
        else:
            log.error(f"Discord webhook error {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"Discord post failed: {e}")
        return False


# ─── Main Pipeline ────────────────────────────────────────────────────────────
async def process_article(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    item: dict,
) -> None:
    url = item["url"]
    if is_duplicate(conn, url):
        return

    aid = article_id(url)
    title = item["title"]
    source = item["source"]
    now = int(time.time())

    # Parse published time
    pub_ts = now
    if item.get("published"):
        try:
            pub_ts = int(time.mktime(item["published"]))
        except Exception:
            pass

    # Skip old articles (> 6 hours)
    if now - pub_ts > CLUSTER_WINDOW:
        return

    # Cheap pre-filter: skip the Claude call entirely if the headline shows
    # no sign of being operational football news. Saves a Claude call on
    # every "5 things we learned" / "tactical analysis" / match preview.
    if not _looks_operational(title):
        log.info(f"SKIP non-operational: {title[:80]}")
        return

    # Extract article body. We deliberately do NOT use the article's
    # og:image — major outlets brand their share previews with watermarks
    # (BBC Sport bug, L'Équipe title cards, Sky bug, etc.) and the preview
    # image often isn't the actual subject of the story. Human operator
    # picks the article-side photo. Logos handled separately (Phase 2).
    body = await extract_article_text(client, url)
    image_url = None

    # Detect language
    try:
        lang = detect(title + " " + body[:200])
    except Exception:
        lang = "en"

    # Detect league from title
    league, club = detect_league(title + " " + body[:500])

    # Process with Claude
    result = process_with_claude(title, body, lang)
    if not result:
        log.debug(f"Skipping {url} — Claude returned nothing")
        return

    title_en = result.get("title_en", title)[:200]
    summary_en = result.get("summary_en", "No summary available.")[:1000]
    urgency = max(1, min(5, int(result.get("urgency", 2))))
    uniqueness = max(1, min(5, int(result.get("uniqueness", 2))))
    tags = result.get("tags", [])

    # Fire CentreGoals draft for the news-tweets channel — immediately, no
    # cap, no clustering. Only fire if the source is in the trusted allowlist
    # (avoids drafting clickbait from low-tier aggregators).
    drafts_webhook = WEBHOOKS.get("tweet_drafts", "")
    if drafts_webhook and urgency >= 2 and _is_trusted_for_drafts(source):
        asyncio.create_task(_draft_article_to_webhook(
            client, conn, drafts_webhook, title_en, summary_en, source, url,
            image_url,
        ))

    # Clustering
    cluster_id = find_or_create_cluster(conn, title_en, league, now)
    already_posted = cluster_already_posted(conn, cluster_id)

    # Get cluster size
    cluster_size = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE cluster_id=?", (cluster_id,)
    ).fetchone()[0] + 1

    # Store article
    conn.execute(
        """INSERT OR IGNORE INTO articles
           (id, url, title_original, title_en, summary_en, source, published_at,
            fetched_at, language, league, club, urgency, uniqueness, cluster_id, posted)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (aid, url, title, title_en, summary_en, source, pub_ts, now,
         lang, league, club, urgency, uniqueness, cluster_id),
    )
    conn.commit()

    # Skip low-value or already-posted clusters
    if urgency < 2 and uniqueness < 2:
        log.info(f"SKIP low-value (u={urgency} q={uniqueness}) {league}: {title_en[:80]}")
        return

    if already_posted and urgency < 3:
        log.info(f"SKIP cluster-dup (u={urgency}) {league}: {title_en[:80]}")
        return

    # Resolve actual webhook route — clubs with their own webhook (e.g.
    # MAN_UTD_WEBHOOK) override the league channel.
    route = route_for(league, club)

    # Hourly cap — breaking (urgency 5) bypasses the cap.
    if urgency < 5 and not hourly_cap_available(conn, route):
        log.info(f"Hourly cap hit for {route} — skipping: {title_en[:60]}")
        return

    # Post to Discord
    webhook_url = WEBHOOKS.get(route, WEBHOOKS.get("other", ""))
    article_data = {
        "title_en": title_en,
        "summary_en": summary_en,
        "url": url,
        "source": source,
        "language": lang,
        "urgency": urgency,
        "uniqueness": uniqueness,
        "tags": tags,
        "club": club,
    }

    posted = await post_discord_embed(
        client, webhook_url, article_data, route, cluster_size
    )

    if posted:
        conn.execute("UPDATE articles SET posted=1 WHERE id=?", (aid,))
        conn.commit()
        hourly_cap_record(conn, route)


async def run_poll_cycle(
    client: httpx.AsyncClient, conn: sqlite3.Connection
) -> None:
    log.info(f"Starting poll cycle — {len(SEARCH_QUERIES)} queries, "
             f"{len(RSS_SOURCES)} RSS feeds")
    tasks = [fetch_feed(client, q) for q in SEARCH_QUERIES]
    tasks += [fetch_rss(client, s) for s in RSS_SOURCES]
    results = await asyncio.gather(*tasks)

    all_items = []
    seen_urls = set()
    for items in results:
        for item in items:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                all_items.append(item)

    log.info(f"Fetched {len(all_items)} unique feed items")

    # Process with concurrency limit
    semaphore = asyncio.Semaphore(5)

    async def bounded(item):
        async with semaphore:
            await process_article(client, conn, item)

    await asyncio.gather(*[bounded(item) for item in all_items])
    log.info("Poll cycle complete")


async def poll_loop(client: httpx.AsyncClient, conn: sqlite3.Connection) -> None:
    while True:
        try:
            await run_poll_cycle(client, conn)
        except Exception as e:
            log.error(f"Poll cycle error: {e}", exc_info=True)
        log.info(f"Sleeping {POLL_INTERVAL}s until next poll...")
        await asyncio.sleep(POLL_INTERVAL)


async def main():
    log.info("⚽ Football Ops Alert Bot starting...")

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set!")
        return

    webhook_count = sum(1 for v in WEBHOOKS.values() if v)
    log.info(f"Configured {webhook_count}/{len(WEBHOOKS)} Discord webhooks")

    conn = init_db()
    log.info(f"Database initialised: {DB_PATH}")

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FootballOpsBot/1.0)",
        "Accept-Language": "en-GB,en;q=0.9",
    }

    drafts_webhook = WEBHOOKS.get("tweet_drafts", "")
    log.info("=" * 60)
    log.info(f"X_BEARER_TOKEN set:      {'YES' if X_BEARER_TOKEN else 'NO'}")
    log.info(f"TWEET_DRAFTS_WEBHOOK set: {'YES' if drafts_webhook else 'NO'}")
    log.info(f"League webhooks set:     "
             f"{sum(1 for k,v in WEBHOOKS.items() if k != 'tweet_drafts' and v)}/6")
    log.info("=" * 60)

    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [asyncio.create_task(poll_loop(client, conn))]

        if X_BEARER_TOKEN and drafts_webhook:
            tweet_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
            loop = asyncio.get_running_loop()
            try:
                x_stream.start_stream(X_BEARER_TOKEN, loop, tweet_queue)
                log.info(f"X filtered stream started — "
                         f"{len(x_stream.JOURNALISTS)} handles")
                tasks.append(asyncio.create_task(tweet_drafter.consume_stream(
                    tweet_queue,
                    claude_client,
                    client,
                    drafts_webhook,
                    dedup_check=lambda sid: draft_already_posted(conn, sid),
                    dedup_record=lambda sid: record_draft_posted(conn, sid),
                )))
            except Exception as e:
                log.error(f"Failed to start X stream: {e}", exc_info=True)
        else:
            log.info("X stream disabled (X_BEARER_TOKEN or "
                     "TWEET_DRAFTS_WEBHOOK missing)")

        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
