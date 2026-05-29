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
}

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")

# Per-webhook hourly post caps (rolling 1h window).
HOURLY_CAPS = {
    "premier_league": 8,
    "la_liga": 6,
    "serie_a": 6,
    "bundesliga": 5,
    "ligue_1": 5,
    "other": 6,
    "tweet_drafts": 15,
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
# search. Broken feeds (O Jogo, SofaScore, Squawka) intentionally omitted;
# L'Équipe and Calciomercato use working endpoints.
RSS_SOURCES: list[dict] = [
    {"name": "BBC Sport Football", "url": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
    {"name": "Guardian Football", "url": "https://www.theguardian.com/football/rss"},
    {"name": "Sky Sports Football", "url": "https://www.skysports.com/rss/12040"},
    {"name": "Independent Football", "url": "https://www.independent.co.uk/sport/football/rss"},
    {"name": "ESPN FC", "url": "https://www.espn.com/espn/rss/soccer/news"},
    {"name": "Goal.com", "url": "https://www.goal.com/feeds/news?fmt=rss"},
    {"name": "Football365", "url": "https://www.football365.com/feed"},
    {"name": "Daily Mail Football", "url": "https://www.dailymail.co.uk/sport/football/index.rss"},
    {"name": "Telegraph Football", "url": "https://www.telegraph.co.uk/football/rss.xml"},
    {"name": "Manchester Evening News", "url": "https://www.manchestereveningnews.co.uk/sport/football/?service=rss"},
    {"name": "Liverpool Echo", "url": "https://www.liverpoolecho.co.uk/sport/football/liverpool-fc/?service=rss"},
    {"name": "L'Équipe", "url": "https://dwh.lequipe.fr/api/edito/rss?path=/Football/"},
    {"name": "RMC Sport", "url": "https://rmcsport.bfmtv.com/rss/football/"},
    {"name": "Get French Football News", "url": "https://www.getfootballnewsfrance.com/feed/"},
    {"name": "Marca (EN)", "url": "https://e00-marca.uecdn.es/rss/en/football/barcelona.xml"},
    {"name": "AS (EN)", "url": "https://en.as.com/rss/an/portada.xml"},
    {"name": "Football España", "url": "https://www.football-espana.net/feed"},
    {"name": "Get Spanish Football News", "url": "https://www.getfootballnewsspain.com/feed/"},
    {"name": "Calciomercato", "url": "https://www.calciomercato.com/rss"},
    {"name": "Football Italia", "url": "https://football-italia.net/feed/"},
    {"name": "Tuttomercatoweb", "url": "https://www.tuttomercatoweb.com/rss"},
    {"name": "Get Italian Football News", "url": "https://www.getfootballnewsitaly.com/feed/"},
    {"name": "Bavarian Football Works", "url": "https://www.bavarianfootballworks.com/rss/current"},
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fetched ON articles(fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster ON articles(cluster_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_league ON articles(league)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_post_log ON post_log(webhook_key, posted_at)")
    conn.commit()
    return conn


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
        log.warning(f"Feed fetch failed for '{query}': {e}")
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
        log.warning(f"RSS fetch failed for {source['name']}: {e}")
        return []


async def extract_article_text(client: httpx.AsyncClient, url: str) -> str:
    """Extract main text from article URL."""
    try:
        resp = await client.get(url, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "ad"]):
            tag.decompose()

        # Try article tag first
        article = soup.find("article")
        if article:
            text = article.get_text(separator=" ", strip=True)
        else:
            # Fallback to paragraphs
            paragraphs = soup.find_all("p")
            text = " ".join(p.get_text(strip=True) for p in paragraphs)

        # Truncate to 3000 chars for API efficiency
        return text[:3000].strip()
    except Exception as e:
        log.debug(f"Article extraction failed for {url}: {e}")
        return ""


# ─── Article ID & Dedup ───────────────────────────────────────────────────────
def article_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def is_duplicate(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
    return row is not None


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
Body: {body[:2500]}

Analyse and return JSON."""
    try:
        msg = claude_client.messages.create(
            model="claude-opus-4-7",
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

    # Extract article body
    body = await extract_article_text(client, url)

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
        log.debug(f"Skipping low-value article: {title_en[:60]}")
        return

    if already_posted and urgency < 4:
        log.debug(f"Cluster already posted, skipping: {title_en[:60]}")
        return

    # Hourly cap — breaking (urgency 5) bypasses the cap.
    if urgency < 5 and not hourly_cap_available(conn, league):
        log.info(f"Hourly cap hit for {league} — skipping: {title_en[:60]}")
        return

    # Post to Discord
    webhook_url = WEBHOOKS.get(league, WEBHOOKS.get("other", ""))
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
        client, webhook_url, article_data, league, cluster_size
    )

    if posted:
        conn.execute("UPDATE articles SET posted=1 WHERE id=?", (aid,))
        conn.commit()
        hourly_cap_record(conn, league)


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

    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [asyncio.create_task(poll_loop(client, conn))]

        drafts_webhook = WEBHOOKS.get("tweet_drafts", "")
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
                    hourly_cap_check=lambda k: hourly_cap_available(conn, k),
                    hourly_cap_record=lambda k: hourly_cap_record(conn, k),
                )))
            except Exception as e:
                log.error(f"Failed to start X stream: {e}", exc_info=True)
        else:
            log.info("X stream disabled (X_BEARER_TOKEN or "
                     "TWEET_DRAFTS_WEBHOOK missing)")

        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
