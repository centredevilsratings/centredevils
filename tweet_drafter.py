"""
CentreGoals Tweet Drafter
Monitors journalists & major outlets, generates pre-written tweets in
CentreGoals voice, posts drafts to a dedicated Discord channel for
copy/paste to X.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import quote_plus

import anthropic
import feedparser
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("tweet-drafter")

POLL_INTERVAL = 90
LOOKBACK_WINDOW = 4 * 3600
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TWEET_DRAFTS_WEBHOOK = os.environ.get("TWEET_DRAFTS_WEBHOOK", "")
MAX_TWEET_CHARS = 280
TARGET_TWEET_CHARS = 240  # headroom for Twitter's weighted counting of bold unicode

# Journalist/outlet specific Google News queries.
# Quoted strings force exact-match so we mostly surface stories these
# journalists/outlets actually authored or are cited in.
JOURNALIST_OUTLET_QUERIES = [
    # Tier-1 transfer journalists
    '"Fabrizio Romano"',
    '"David Ornstein"',
    '"Florian Plettenberg"',
    '"Matteo Moretto"',
    '"Gianluca Di Marzio"',
    '"Ben Jacobs"',
    '"Sacha Tavolieri"',
    '"Nicolo Schira"',
    '"Rudy Galetti"',
    # Major outlets
    'site:bbc.com/sport/football',
    'site:theathletic.com football',
    'site:skysports.com/football',
    'site:espn.com/soccer',
    'site:marca.com futbol',
    'site:as.com futbol',
    'site:gazzetta.it calcio',
    'site:lequipe.fr football',
    'site:bild.de fussball',
    'site:theguardian.com/football',
]

# Source name (as it appears in Google News) -> X handle for attribution
SOURCE_HANDLES = {
    "Fabrizio Romano": "@FabrizioRomano",
    "David Ornstein": "@David_Ornstein",
    "Florian Plettenberg": "@Plettigoal",
    "Matteo Moretto": "@MatteMoretto",
    "Gianluca Di Marzio": "@DiMarzio",
    "Ben Jacobs": "@JacobsBen",
    "Sacha Tavolieri": "@sachatavolieri",
    "Nicolo Schira": "@NicoSchira",
    "Rudy Galetti": "@RudyGaletti",
    "BBC Sport": "@BBCSport",
    "BBC": "@BBCSport",
    "The Athletic": "@TheAthletic",
    "Athletic": "@TheAthletic",
    "Sky Sports": "@SkySportsNews",
    "ESPN": "@ESPNFC",
    "ESPN FC": "@ESPNFC",
    "Marca": "@marca",
    "AS": "@diarioas",
    "Diario AS": "@diarioas",
    "La Gazzetta dello Sport": "@Gazzetta_it",
    "Gazzetta": "@Gazzetta_it",
    "L'Équipe": "@lequipe",
    "L'Equipe": "@lequipe",
    "Bild": "@BILD",
    "The Guardian": "@guardian_sport",
    "Guardian": "@guardian_sport",
    "RMC Sport": "@RMCsport",
    "Bleacher Report": "@brfootball",
    "B/R Football": "@brfootball",
    "OneFootball": "@OneFootball",
    "EuroFoot": "@eurofootcom",
    "Relevo": "@relevo",
}


CENTREGOALS_SYSTEM = """You are a tweet writer for CentreGoals, a football news Twitter account.

Given a football news article, write ONE tweet in the EXACT CentreGoals voice.

VOICE RULES (non-negotiable):
1. Always open with: 🚨🚨|  (two siren emojis, pipe, space)
2. Immediately follow with one of: "BREAKING:" (confirmed breaking news), "JUST IN:" (developing), or no prefix (lists/rankings/analysis)
3. Render the KEY FACT (1-3 words max) in 𝐌𝐀𝐓𝐇𝐄𝐌𝐀𝐓𝐈𝐂𝐀𝐋 𝐁𝐎𝐋𝐃 unicode. Examples: 𝐌𝐔𝐒𝐂𝐋𝐄 𝐈𝐍𝐉𝐔𝐑𝐘, 𝐋𝐄𝐀𝐕𝐄, 𝐑𝐄𝐓𝐈𝐑𝐈𝐍𝐆, 𝐃𝐎𝐍𝐄 𝐃𝐄𝐀𝐋, 𝐒𝐀𝐂𝐊𝐄𝐃, 𝐒𝐈𝐆𝐍𝐄𝐃, 𝐀𝐆𝐑𝐄𝐄𝐃
4. End the first line with a relevant emoji + country flag. Examples: 🤕🇧🇷 (injury), 👋🇺🇸 (departure), ✅🏴󠁧󠁢󠁥󠁮󠁧󠁿 (signing), 🔴⚽ (goal), 🏆 (trophy)
5. Optionally add ONE short context sentence (≤ 20 words) on a NEW paragraph
6. End with the source X handle in square brackets on its own paragraph: [@HandleName]
7. Total length MUST be ≤ 240 characters (counts including the source bracket)
8. NO hashtags. Only the opening 🚨🚨, the flag/reaction at the end of line 1, and any inside quoted text
9. NEVER invent facts — work ONLY with what is explicitly in the article
10. NEVER paraphrase a quote — only use facts (transfers, injuries, statements, contract details)

REFERENCE EXAMPLES:

🚨🚨| BREAKING: Neymar has suffered a 𝐌𝐔𝐒𝐂𝐋𝐄 𝐈𝐍𝐉𝐔𝐑𝐘 and will be out for 2-3 weeks after tests confirmed the issue. 🤕🇧🇷

He will miss Brazil's pre-World Cup friendlies and could also miss the opening game against Morocco.

[@FabrizioRomano]

---

🚨🚨| BREAKING: Luka Modrić 𝐑𝐄𝐓𝐈𝐑𝐈𝐍𝐆 after the World Cup is a possibility. 👋🇭🇷

[@diarioas]

---

🚨🚨| JUST IN: Mauricio Pochettino will 𝐋𝐄𝐀𝐕𝐄 USA National Team after the World Cup. 👋🇺🇸

He has been offered to AC Milan by an intermediary in the last hours.

[@NicoSchira]

---

OUTPUT (JSON only, no markdown fences):
{
  "tweet": "the full tweet text following voice rules exactly",
  "should_post": <true if newsworthy CentreGoals would tweet this, false if filler/opinion/non-news>,
  "category": "<one of: breaking_transfer, confirmed_signing, injury, manager, contract, rumour, ranking, other>",
  "reason_if_skip": "<short reason if should_post is false, else empty string>"
}"""


def init_drafter_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tweet_drafts (
            id TEXT PRIMARY KEY,
            url TEXT UNIQUE,
            source TEXT,
            handle TEXT,
            tweet TEXT,
            category TEXT,
            posted INTEGER DEFAULT 0,
            created_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_draft_created ON tweet_drafts(created_at)")
    conn.commit()
    return conn


def build_rss_url(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-GB&gl=GB&ceid=GB:en"


def article_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def is_seen(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute("SELECT id FROM tweet_drafts WHERE url=?", (url,)).fetchone()
    return row is not None


def resolve_handle(source_name: str) -> str:
    """Map a Google News source name to an X handle. Falls back to a slugified guess."""
    if not source_name:
        return "@source"
    # Exact match first
    if source_name in SOURCE_HANDLES:
        return SOURCE_HANDLES[source_name]
    # Substring match (Google News sometimes adds suffixes like "- Football")
    for known, handle in SOURCE_HANDLES.items():
        if known.lower() in source_name.lower():
            return handle
    # Fallback: strip non-alnum and prefix @
    slug = re.sub(r"[^A-Za-z0-9]", "", source_name)[:15]
    return f"@{slug}" if slug else "@source"


async def fetch_feed(client: httpx.AsyncClient, query: str) -> list[dict]:
    url = build_rss_url(query)
    try:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        items = []
        for entry in feed.entries[:8]:
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
        log.warning(f"Drafter feed fetch failed for '{query}': {e}")
        return []


async def extract_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        article = soup.find("article")
        if article:
            text = article.get_text(separator=" ", strip=True)
        else:
            text = " ".join(p.get_text(strip=True) for p in soup.find_all("p"))
        return text[:2500].strip()
    except Exception as e:
        log.debug(f"Drafter extraction failed for {url}: {e}")
        return ""


_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def draft_tweet(title: str, body: str, source_handle: str) -> dict | None:
    if not _claude:
        return None
    user_prompt = f"""Article title: {title}
Article body: {body[:2200]}
Source X handle to attribute: {source_handle}

Write the tweet now. Return JSON only."""
    try:
        msg = _claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=CENTREGOALS_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        log.warning(f"Tweet draft generation failed: {e}")
        return None


async def post_draft_to_discord(
    client: httpx.AsyncClient,
    tweet: str,
    article: dict,
    category: str,
) -> bool:
    if not TWEET_DRAFTS_WEBHOOK:
        log.warning("TWEET_DRAFTS_WEBHOOK not configured — skipping draft post")
        return False

    char_count = len(tweet)
    over_limit = char_count > MAX_TWEET_CHARS

    embed = {
        "title": "📝 Tweet Draft Ready",
        "description": f"```\n{tweet}\n```",
        "color": 0xE74C3C if over_limit else 0x1DA1F2,
        "fields": [
            {"name": "Category", "value": category.replace("_", " ").title(), "inline": True},
            {
                "name": "Length",
                "value": f"{char_count}/{MAX_TWEET_CHARS}" + (" ⚠️ OVER" if over_limit else ""),
                "inline": True,
            },
            {"name": "Source", "value": f"[Article]({article['url']})", "inline": True},
        ],
        "footer": {"text": f"From: {article.get('source', 'Unknown')}"},
        "timestamp": datetime.utcnow().isoformat(),
    }

    payload = {"embeds": [embed]}
    try:
        resp = await client.post(TWEET_DRAFTS_WEBHOOK, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info(f"Posted tweet draft ({char_count} chars, {category})")
            return True
        log.error(f"Drafts webhook error {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Drafts webhook post failed: {e}")
        return False


async def process_drafter_item(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    item: dict,
) -> None:
    url = item["url"]
    if is_seen(conn, url):
        return

    now = int(time.time())
    pub_ts = now
    if item.get("published"):
        try:
            pub_ts = int(time.mktime(item["published"]))
        except Exception:
            pass

    # Only draft for fresh stories — drafts are useless if old
    if now - pub_ts > LOOKBACK_WINDOW:
        return

    body = await extract_text(client, url)
    if len(body) < 200:
        return

    handle = resolve_handle(item.get("source", ""))
    result = draft_tweet(item["title"], body, handle)
    if not result:
        return

    if not result.get("should_post", False):
        log.debug(f"Drafter skipped: {result.get('reason_if_skip', 'low value')} — {item['title'][:60]}")
        # Still record so we don't reprocess
        conn.execute(
            "INSERT OR IGNORE INTO tweet_drafts(id, url, source, handle, tweet, category, posted, created_at) VALUES(?,?,?,?,?,?,0,?)",
            (article_id(url), url, item.get("source", ""), handle, "", result.get("category", "skipped"), now),
        )
        conn.commit()
        return

    tweet = result.get("tweet", "").strip()
    category = result.get("category", "other")

    if not tweet:
        return

    posted = await post_draft_to_discord(client, tweet, item, category)

    conn.execute(
        "INSERT OR IGNORE INTO tweet_drafts(id, url, source, handle, tweet, category, posted, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (article_id(url), url, item.get("source", ""), handle, tweet, category, 1 if posted else 0, now),
    )
    conn.commit()


async def drafter_poll_cycle(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
) -> None:
    log.info(f"Drafter cycle starting — {len(JOURNALIST_OUTLET_QUERIES)} queries")
    results = await asyncio.gather(*[fetch_feed(client, q) for q in JOURNALIST_OUTLET_QUERIES])

    seen_urls = set()
    items = []
    for batch in results:
        for it in batch:
            if it["url"] not in seen_urls:
                seen_urls.add(it["url"])
                items.append(it)

    log.info(f"Drafter found {len(items)} unique items")
    sem = asyncio.Semaphore(4)

    async def bounded(it):
        async with sem:
            await process_drafter_item(client, conn, it)

    await asyncio.gather(*[bounded(it) for it in items])
    log.info("Drafter cycle complete")


async def run_drafter_loop(db_path: str) -> None:
    if not TWEET_DRAFTS_WEBHOOK:
        log.warning("TWEET_DRAFTS_WEBHOOK not set — drafter loop disabled")
        return
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set — drafter loop disabled")
        return

    conn = init_drafter_db(db_path)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CentreGoalsDrafter/1.0)",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        while True:
            try:
                await drafter_poll_cycle(client, conn)
            except Exception as e:
                log.error(f"Drafter cycle error: {e}", exc_info=True)
            log.info(f"Drafter sleeping {POLL_INTERVAL}s")
            await asyncio.sleep(POLL_INTERVAL)
