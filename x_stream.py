"""
X (Twitter) Filtered Stream Listener
Connects to X API filtered stream for top football journalists & outlets.
On each new tweet: generates a CentreGoals-voice draft via Claude and posts
to the tweet drafts Discord webhook — within seconds of the original tweet.

This is the "be FIRST" path. RSS-based drafter in tweet_drafter.py is the
complementary slower path for outlets that don't post to X first.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime

import anthropic
import httpx

log = logging.getLogger("x-stream")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN", "")
TWEET_DRAFTS_WEBHOOK = os.environ.get("TWEET_DRAFTS_WEBHOOK", "")
DB_PATH = os.environ.get("DB_PATH", "football_ops.db")

X_API_BASE = "https://api.twitter.com/2"
RECONNECT_DELAY_INITIAL = 5
RECONNECT_DELAY_MAX = 300

# Tier 1: top transfer-scoop journalists (global)
TIER_1_JOURNALISTS = [
    "FabrizioRomano",
    "David_Ornstein",
    "Plettigoal",          # Florian Plettenberg
    "MatteMoretto",        # Matteo Moretto
    "DiMarzio",            # Gianluca Di Marzio
    "JacobsBen",           # Ben Jacobs
    "sachatavolieri",
    "NicoSchira",
    "RudyGaletti",
    "GuillemBalague",
    "Santi_J_FM",
    "cfbayern",            # Christian Falk (Bild transfer scoops)
    "FabriceHawkins",      # RMC Sport transfers
    "MartynZiegler",       # The Times
    "SamiMokbel81",        # Sami Mokbel (Daily Mail)
]

# Club / league beat reporters
CLUB_REPORTERS = [
    # Man Utd
    "samuelluckhurst",     # Samuel Luckhurst (MEN)
    "ChrisWheelerDM",      # Chris Wheeler (Daily Mail Man Utd)
    "RobDawsonESPN",       # Rob Dawson (ESPN Man Utd)
    "ChrisWheatley_",      # Chris Wheatley
    # Liverpool
    "PhilHay_",
    "JamesPearceLFC",
    # Sky Sports News
    "KaveSolhekol",
    # ESPN La Liga
    "LaurensJulien",
    # Spain
    "MarioCortegana",
    "GuillermoRai_",       # Real Madrid beat
    "gerardromero",        # Barça
    # Bayern Munich
    "iMiaSanMia",
]

# Aggregator brands & club-specific aggregators
AGGREGATORS = [
    # Rivals (so we can see what they post)
    "TouchlineX",
    "DeadlineDayLive",
    "AlbicelesteTalk",
    # Club aggregators
    "MadridZone",
    "MadridXtra",
    "ManagingBarca",
    "PSGINT_",
    "atletiuniverse",
    "AlNassrZone",
    "TotalCristiano",
    "mufcMPB",
]

# Major outlet brand accounts
MAJOR_OUTLETS = [
    "TheAthleticFC",
    "BBCSport",
    "SkySportsNews",
    "SkySportsPL",
    "ESPNFC",
    "marca",
    "diarioas",
    "Gazzetta_it",
    "lequipe",
    "BILD",
    "relevo",
    "RMCsport",
    "brfootball",
    "OneFootball",
    "BeFootball",
    "eurofootcom",
]

# Non-English aggregators
REGIONAL_AGGREGATORS = [
    "ActuFoot_",           # French aggregator
    "vibesfoot",           # French aggregator
    "ActuSPL",             # Saudi Pro League aggregator
]

# Our own handle — exclude from drafting so we don't redraft our own tweets.
SELF_HANDLES = {"centredevils"}

# Combined target list (deduped, excluding self)
ALL_TARGET_HANDLES = []
_seen = set()
for _group in (TIER_1_JOURNALISTS, CLUB_REPORTERS, AGGREGATORS, MAJOR_OUTLETS, REGIONAL_AGGREGATORS):
    for _h in _group:
        if _h.lower() in SELF_HANDLES or _h.lower() in _seen:
            continue
        _seen.add(_h.lower())
        ALL_TARGET_HANDLES.append(_h)

# Handles whose tweets are usually trustworthy enough to draft immediately
HIGH_TRUST_HANDLES = {h.lower() for h in TIER_1_JOURNALISTS}


def chunked_or_rule(handles: list[str], tag: str) -> list[dict]:
    """Build filtered-stream rules. Each rule capped at ~900 chars
    (X Basic-tier limit is 1024; leave headroom).
    Note: NO lang filter — Claude translates, and lang filtering with OR
    has tricky precedence in X's query syntax."""
    rules: list[dict] = []
    parts: list[str] = []
    current_len = 0
    suffix = " -is:retweet -is:reply"
    for h in handles:
        chunk = f"from:{h}"
        added = (4 if parts else 0) + len(chunk)
        if current_len + added + len(suffix) + 2 > 900 and parts:
            value = "(" + " OR ".join(parts) + ")" + suffix
            rules.append({"value": value, "tag": tag})
            parts = [chunk]
            current_len = len(chunk)
        else:
            parts.append(chunk)
            current_len += added
    if parts:
        value = "(" + " OR ".join(parts) + ")" + suffix
        rules.append({"value": value, "tag": tag})
    return rules


CENTREGOALS_SYSTEM = """You are a tweet writer for CentreGoals, a football news Twitter account.

You are given a SOURCE TWEET from a trusted football journalist or outlet. Rewrite it in the EXACT CentreGoals voice as a fresh tweet attributed to the source. Speed matters — be punchy and fact-only.

VOICE RULES (non-negotiable):
1. Always open with: 🚨🚨|  (two siren emojis, pipe, space)
2. Immediately follow with: "BREAKING:" (confirmed scoop), "JUST IN:" (developing), or no prefix (lists/rankings/analysis)
3. Render the KEY FACT (1-3 words max) in 𝐌𝐀𝐓𝐇𝐄𝐌𝐀𝐓𝐈𝐂𝐀𝐋 𝐁𝐎𝐋𝐃 unicode. Examples: 𝐌𝐔𝐒𝐂𝐋𝐄 𝐈𝐍𝐉𝐔𝐑𝐘, 𝐋𝐄𝐀𝐕𝐄, 𝐑𝐄𝐓𝐈𝐑𝐈𝐍𝐆, 𝐃𝐎𝐍𝐄 𝐃𝐄𝐀𝐋, 𝐒𝐀𝐂𝐊𝐄𝐃, 𝐒𝐈𝐆𝐍𝐄𝐃, 𝐀𝐆𝐑𝐄𝐄𝐃, 𝐇𝐄𝐑𝐄 𝐖𝐄 𝐆𝐎
4. End line 1 with relevant emoji + country flag. Examples: 🤕🇧🇷 (injury), 👋🇺🇸 (departure), ✅🏴󠁧󠁢󠁥󠁮󠁧󠁿 (signing), 🔴⚽ (goal), 🏆 (trophy)
5. Optionally ONE short context sentence (≤ 20 words) on a NEW paragraph
6. End with the source X handle in square brackets on its own paragraph: [@HandleName]
7. Total length ≤ 240 characters including the source bracket
8. NO hashtags. Only the opening 🚨🚨, the flag/reaction at end of line 1, and any inside quoted text
9. NEVER invent facts. Only use facts EXPLICITLY in the source tweet
10. If the source tweet is non-news (banter, opinion, retweet-comment without fact, promotional, video without info, foreign-language press review with no concrete fact), set should_post=false
11. Translate non-English source tweets to English. Preserve all proper nouns and numbers exactly.

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
  "should_post": <true if newsworthy, false if filler/opinion/non-news>,
  "category": "<one of: breaking_transfer, confirmed_signing, injury, manager, contract, rumour, ranking, match_news, other>",
  "reason_if_skip": "<short reason if should_post is false, else empty string>"
}"""


_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def init_x_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS x_tweets (
            tweet_id TEXT PRIMARY KEY,
            author_handle TEXT,
            text_original TEXT,
            text_draft TEXT,
            category TEXT,
            posted INTEGER DEFAULT 0,
            received_at INTEGER,
            posted_at INTEGER,
            latency_ms INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_x_received ON x_tweets(received_at)")
    conn.commit()
    return conn


def seen_tweet(conn: sqlite3.Connection, tweet_id: str) -> bool:
    return conn.execute("SELECT 1 FROM x_tweets WHERE tweet_id=?", (tweet_id,)).fetchone() is not None


async def get_existing_rules(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(
        f"{X_API_BASE}/tweets/search/stream/rules",
        headers={"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data", []) or []


async def delete_rules(client: httpx.AsyncClient, ids: list[str]) -> None:
    if not ids:
        return
    resp = await client.post(
        f"{X_API_BASE}/tweets/search/stream/rules",
        headers={"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"},
        json={"delete": {"ids": ids}},
        timeout=15,
    )
    resp.raise_for_status()


async def add_rules(client: httpx.AsyncClient, rules: list[dict]) -> None:
    resp = await client.post(
        f"{X_API_BASE}/tweets/search/stream/rules",
        headers={"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"},
        json={"add": rules},
        timeout=15,
    )
    if resp.status_code >= 400:
        log.error(f"Rule add failed {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        for err in body["errors"]:
            log.warning(f"Rule error: {err}")
    log.info(f"Installed {len(rules)} stream rules: {body.get('meta', {})}")


async def sync_stream_rules(client: httpx.AsyncClient) -> None:
    desired = (
        chunked_or_rule(TIER_1_JOURNALISTS, "tier1_journos")
        + chunked_or_rule(CLUB_REPORTERS, "club_reporters")
        + chunked_or_rule(AGGREGATORS, "aggregators")
        + chunked_or_rule(MAJOR_OUTLETS, "outlets")
        + chunked_or_rule(REGIONAL_AGGREGATORS, "regional")
    )
    log.info(
        f"Syncing {len(desired)} stream rules across {len(ALL_TARGET_HANDLES)} unique handles"
    )
    existing = await get_existing_rules(client)
    if existing:
        log.info(f"Removing {len(existing)} existing rules")
        await delete_rules(client, [r["id"] for r in existing])
    await add_rules(client, desired)


def draft_centregoals_tweet(source_text: str, source_handle: str) -> dict | None:
    if not _claude:
        return None
    user_prompt = f"""SOURCE TWEET from @{source_handle}:
\"\"\"
{source_text}
\"\"\"

Rewrite as a CentreGoals tweet. Attribute source as [@{source_handle}]. Return JSON only."""
    try:
        msg = _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=CENTREGOALS_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        log.warning(f"Claude draft failed for tweet from @{source_handle}: {e}")
        return None


RIVAL_HANDLES = {"touchlinex", "deadlinedaylive", "albicelestetalk"}


def classify_source(handle: str) -> tuple[str, int]:
    """Return (badge, color_hint) for the source handle."""
    h = handle.lower()
    if h in RIVAL_HANDLES:
        return "⚔️ RIVAL", 0x9B59B6  # purple — rival is breaking
    if h in HIGH_TRUST_HANDLES:
        return "⭐ TIER 1", 0xF1C40F  # gold — top journo
    return "📰 SOURCE", 0x1DA1F2     # twitter blue — default


async def post_draft(
    client: httpx.AsyncClient,
    tweet_text: str,
    source_handle: str,
    source_tweet_id: str,
    source_text: str,
    category: str,
    latency_ms: int,
) -> bool:
    if not TWEET_DRAFTS_WEBHOOK:
        return False
    char_count = len(tweet_text)
    over_limit = char_count > 280
    source_url = f"https://x.com/{source_handle}/status/{source_tweet_id}"
    badge, color = classify_source(source_handle)
    if over_limit:
        color = 0xE74C3C

    embed = {
        "title": f"{badge} — Tweet Draft Ready",
        "description": f"```\n{tweet_text}\n```",
        "color": color,
        "fields": [
            {"name": "Category", "value": category.replace("_", " ").title(), "inline": True},
            {
                "name": "Length",
                "value": f"{char_count}/280" + (" ⚠️ OVER" if over_limit else ""),
                "inline": True,
            },
            {"name": "Latency", "value": f"{latency_ms / 1000:.1f}s", "inline": True},
            {"name": "Source", "value": f"[@{source_handle}]({source_url})", "inline": False},
            {"name": "Original", "value": source_text[:1000], "inline": False},
        ],
        "footer": {"text": "🔴 LIVE from X stream"},
        "timestamp": datetime.utcnow().isoformat(),
    }
    payload = {"embeds": [embed]}
    try:
        resp = await client.post(TWEET_DRAFTS_WEBHOOK, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info(f"Drafted from @{source_handle} in {latency_ms}ms — {category}")
            return True
        log.error(f"Drafts webhook error {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Drafts webhook post failed: {e}")
        return False


async def handle_tweet_event(
    post_client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    payload: dict,
) -> None:
    received_at = time.time()
    data = payload.get("data") or {}
    tweet_id = data.get("id")
    text = data.get("text", "")
    if not tweet_id or not text:
        return
    if seen_tweet(conn, tweet_id):
        return

    includes = payload.get("includes") or {}
    users = includes.get("users") or []
    author = users[0] if users else {}
    handle = author.get("username", "unknown")

    # Generate draft (this is the slow step — ~1-3s on haiku)
    result = await asyncio.to_thread(draft_centregoals_tweet, text, handle)
    if not result:
        return

    if not result.get("should_post", False):
        log.debug(f"X stream skipped @{handle}: {result.get('reason_if_skip', 'low value')}")
        conn.execute(
            "INSERT OR IGNORE INTO x_tweets(tweet_id, author_handle, text_original, text_draft, category, posted, received_at) VALUES(?,?,?,?,?,0,?)",
            (tweet_id, handle, text, "", result.get("category", "skipped"), int(received_at)),
        )
        conn.commit()
        return

    draft_text = result.get("tweet", "").strip()
    if not draft_text:
        return
    category = result.get("category", "other")
    latency_ms = int((time.time() - received_at) * 1000)

    posted = await post_draft(
        post_client, draft_text, handle, tweet_id, text, category, latency_ms
    )

    conn.execute(
        "INSERT OR IGNORE INTO x_tweets(tweet_id, author_handle, text_original, text_draft, category, posted, received_at, posted_at, latency_ms) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            tweet_id, handle, text, draft_text, category,
            1 if posted else 0, int(received_at),
            int(time.time()) if posted else None,
            latency_ms,
        ),
    )
    conn.commit()


async def stream_loop(conn: sqlite3.Connection) -> None:
    """Open the filtered stream and dispatch events.
    Auto-reconnects with exponential backoff on disconnect."""
    backoff = RECONNECT_DELAY_INITIAL
    stream_params = {
        "expansions": "author_id",
        "tweet.fields": "created_at,lang,referenced_tweets",
        "user.fields": "username,name",
    }
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=20)) as post_client:
        # Ensure stream rules are installed (do this once per session)
        try:
            await sync_stream_rules(post_client)
        except Exception as e:
            log.error(f"Failed to sync stream rules: {e}", exc_info=True)
            return

        while True:
            try:
                log.info("Connecting to X filtered stream...")
                async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=20)) as stream_client:
                    async with stream_client.stream(
                        "GET",
                        f"{X_API_BASE}/tweets/search/stream",
                        headers=headers,
                        params=stream_params,
                    ) as resp:
                        if resp.status_code != 200:
                            err_body = await resp.aread()
                            log.error(f"Stream open failed {resp.status_code}: {err_body[:300]}")
                            if resp.status_code in (401, 403):
                                log.error("Auth/permission error — check TWITTER_BEARER_TOKEN and tier (streaming requires Basic+)")
                                return
                            raise httpx.HTTPError(f"Stream HTTP {resp.status_code}")

                        log.info("✅ X stream connected — listening for tweets")
                        backoff = RECONNECT_DELAY_INITIAL
                        async for line in resp.aiter_lines():
                            if not line.strip():
                                continue
                            try:
                                payload = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            # Process in background so the stream keeps draining
                            asyncio.create_task(handle_tweet_event(post_client, conn, payload))
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                log.warning(f"Stream disconnected: {e} — reconnecting in {backoff}s")
            except Exception as e:
                log.error(f"Stream loop unexpected error: {e}", exc_info=True)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_DELAY_MAX)


async def run_x_stream(db_path: str) -> None:
    if not TWITTER_BEARER_TOKEN:
        log.warning("TWITTER_BEARER_TOKEN not set — X stream disabled")
        return
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set — X stream disabled")
        return
    if not TWEET_DRAFTS_WEBHOOK:
        log.warning("TWEET_DRAFTS_WEBHOOK not set — X stream disabled")
        return

    conn = init_x_db(db_path)
    log.info(
        f"X stream starting — tracking {len(ALL_TARGET_HANDLES)} handles "
        f"({len(TIER_1_JOURNALISTS)} tier-1, {len(CLUB_REPORTERS)} club reporters, "
        f"{len(AGGREGATORS)} aggregators, {len(MAJOR_OUTLETS)} outlets, "
        f"{len(REGIONAL_AGGREGATORS)} regional)"
    )
    await stream_loop(conn)
