"""
X (Twitter) ingestion for football journalists.

Two producers feed the same asyncio.Queue consumed by the drafter:

  * poll_recent_tweets() — the PRIMARY path. Polls GET /2/tweets/search/recent
    on an interval. Plain REST requests, so it has NO single-connection limit
    (the filtered stream is capped at ONE connection per token and kept
    hitting 429 TooManyConnections across redeploys / multiple instances).
    Survives redeploys cleanly and can't get "too many connections".

  * the tweepy filtered-stream code below is retained but NO LONGER WIRED UP
    (bot.py uses the poller). Kept for reference / quick rollback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import deque
from typing import Iterable, Optional

import httpx
import tweepy

log = logging.getLogger("football-bot.x_stream")

# Poll cadence (seconds). Priority accounts (stats + tier-1 breakers) are
# polled fast for head-turning in-game content; everyone else slower. Both
# are env-overridable so the cadence can be tuned to the X API tier's rate
# limit without a code change.
PRIORITY_INTERVAL = int(os.environ.get("X_POLL_PRIORITY_INTERVAL", "70"))
FULL_INTERVAL = int(os.environ.get("X_POLL_FULL_INTERVAL", "200"))

_SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"


# ~95 football journalists, outlets, aggregators & rivals.
# Handles only — no leading @.
JOURNALISTS: list[str] = [
    # ── Tier-1 transfer reporters (global scoop-breakers) ──
    "FabrizioRomano", "David_Ornstein", "DiMarzio", "NicoSchira",
    "JacobsBen", "Plettigoal", "MatteMoretto", "sachatavolieri",
    "RudyGaletti", "cfbayern", "FabriceHawkins", "MartynZiegler",
    "SamiMokbel81", "SkyKaveh", "GeoffShreeves", "SkySportsLyall",

    # ── English club beat writers ──
    "Matt_Law_DT", "johncrossmirror", "JamesPearceLFC", "PhilHay_",
    "dhytner", "JamieJackson___", "sistoney67", "lauriewhitwell",
    "AdamCrafton_", "henrywinter", "miguel_delaney", "OliverKay",
    "DTathletic", "honigstein", "tariqpanja", "sidlowe",
    "GuillemBalague", "samuelluckhurst", "ChrisWheelerDM",
    "RobDawsonESPN", "ChrisWheatley_",

    # ── French press ──
    "manulonjon", "Tanziloic", "mohamedbouhafsi", "RomainMolina",
    "hugoguillemet",

    # ── Spanish press ──
    "Santi_J_FM", "MarioCortegana", "GuillermoRai_", "gerardromero",

    # ── Argentine / South-American press (Messi, Argentina, Conmebol) ──
    "gastonedul",       # Gastón Edul — TyC Sports, primary Messi/Argentina source
    "CesarLuisMerlo",   # César Luis Merlo — major Argentine transfer/news
    "EzeQuintana",      # Ezequiel Quintana — Argentine football reporter
    "TyCSports",        # Argentine sports network
    "ESPNArgentina",    # ESPN Argentina
    "tntsports",        # TNT Sports Brazil (Brazil-side coverage)

    # ── International / World Cup official ──
    "FIFAWorldCup",     # FIFA World Cup official
    "FIFAcom",          # FIFA main
    "Argentina",        # AFA Argentina official

    # ── Outlets — Spanish ──
    "marca", "diarioas", "mundodeportivo", "sport", "relevo",

    # ── Outlets — Italian ──
    "Gazzetta_it", "tuttosport",

    # ── Outlets — German ──
    "BILD_Sport", "SPORTBILD", "kicker",

    # ── Outlets — French ──
    "lequipe", "RMCsport",

    # ── Outlets — UK / international ──
    "SkySportsNews", "BBCSport", "BBCFootball", "TheAthleticFC",
    "ESPNFC", "TeleFootball", "guardian_sport", "goal",
    "GetFootballNews", "itvfootball", "talkSPORT", "SunSport",
    "SkySport",

    # ── Modern aggregator outlets ──
    "brfootball", "OneFootball", "_BeFootball", "eurofootcom",

    # ── Rival aggregators (track to see what they break) ──
    "TouchlineX", "DeadlineDayLive", "AlbicelesteTalk",

    # ── Club / fan aggregator accounts ──
    "theMadridZone", "MadridXtra", "ManagingBarca", "atletiuniverse",
    "PSGINT_", "iMiaSanMia", "AlNassrZone", "TotalCristiano",
    "mufcMPB",

    # ── Regional / language aggregators ──
    "ActuFoot_", "vibesfoot", "ActuSPL",

    # ── Stats accounts (feed RECORD drafts) ──
    "OptaJoe",          # Premier League (Opta UK)
    "OptaJose",         # La Liga (Opta Spain)
    "OptaPaolo",        # Serie A (Opta Italy)
    "OptaFranz",        # Bundesliga (Opta Germany)
    "OptaJean",         # Ligue 1 (Opta France)
    "OptaJoao",         # Primeira Liga (Opta Portugal)
    "OptaAnalyst",      # Opta long-form analysis
    "OptaFacts",        # Opta factoids / cross-league
    "Squawka",          # Squawka stats
    "SquawkaNews",      # Squawka news
    "WhoScored",        # WhoScored
    "SofascoreINT",     # Sofascore international
    "StatMuse",         # StatMuse
    "StatsBomb",        # StatsBomb analytics
    "InfogolApp",       # Infogol stats
    "MisterChip",       # Alexis Martín-Tamayo, Spanish stats guru
]

# High-priority accounts polled on the FAST cadence — the head-turning
# in-game stats and the tier-1 breakers. These are the ones that need to
# land in #news-tweets within ~a minute during matches. All are a subset of
# JOURNALISTS above; the full list is polled on the slower cadence minus
# these (so no account is polled by both loops).
PRIORITY_HANDLES: list[str] = [
    # Live stats (the "head-turning stat during the game" accounts)
    "OptaJoe", "OptaJose", "OptaPaolo", "OptaFranz", "OptaJean", "OptaJoao",
    "OptaAnalyst", "OptaFacts", "Squawka", "SquawkaNews", "WhoScored",
    "SofascoreINT", "StatMuse", "StatsBomb", "InfogolApp", "MisterChip",
    # Tier-1 transfer / breaking reporters
    "FabrizioRomano", "David_Ornstein", "DiMarzio", "NicoSchira",
    "Plettigoal", "MatteMoretto",
    # World Cup / Argentina real-time
    "gastonedul", "CesarLuisMerlo", "TyCSports", "FIFAWorldCup",
    # Fast-breaking aggregators
    "brfootball", "OneFootball", "TouchlineX", "DeadlineDayLive",
]


def _chunk_rules(handles: Iterable[str], max_len: int = 512) -> list[str]:
    """Build OR-joined `from:` rules, each under the per-rule character limit
    (X allows 512 chars per filtered-stream rule)."""
    rules: list[str] = []
    current: list[str] = []
    current_len = 0
    for h in handles:
        clause = f"from:{h}"
        extra = len(clause) + (4 if current else 0)  # " OR "
        if current_len + extra > max_len:
            rules.append(" OR ".join(current))
            current = [clause]
            current_len = len(clause)
        else:
            current.append(clause)
            current_len += extra
    if current:
        rules.append(" OR ".join(current))
    return rules


class _Listener(tweepy.StreamingClient):
    def __init__(self, bearer_token: str, loop: asyncio.AbstractEventLoop,
                 queue: asyncio.Queue):
        super().__init__(bearer_token, wait_on_rate_limit=True)
        self._loop = loop
        self._queue = queue

    def on_response(self, response):  # type: ignore[override]
        tweet = response.data
        if tweet is None:
            return

        # Skip retweets, replies and quote-tweets — we want original posts.
        for ref in (tweet.referenced_tweets or []):
            if ref.type in ("retweeted", "replied_to", "quoted"):
                return

        includes = response.includes or {}
        users = {u.id: u for u in includes.get("users", [])}
        author = users.get(tweet.author_id)
        if not author:
            return

        # Capture the first attached photo, if any — the tweet's own media.
        # Strictly photos only: videos and animated GIFs expose a
        # preview_image_url that's just a still frame (often a mid-action
        # blur or a stadium wide shot) — those are NOT usable as the
        # story photo. Operator picks a clean photo manually instead.
        image_url = None
        media_items = includes.get("media", [])
        media_map = {m.media_key: m for m in media_items}
        keys = []
        if tweet.attachments:
            keys = (tweet.attachments.get("media_keys")
                    if isinstance(tweet.attachments, dict)
                    else getattr(tweet.attachments, "media_keys", [])) or []
        for key in keys:
            m = media_map.get(key)
            if not m:
                continue
            if getattr(m, "type", None) == "photo" and getattr(m, "url", None):
                image_url = m.url
                break

        event = {
            "id": str(tweet.id),
            "text": tweet.text,
            "handle": author.username,
            "author_name": author.name,
            "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
            "image_url": image_url,
        }
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def on_connect(self):  # type: ignore[override]
        log.info("X stream CONNECTED — real-time tweets flowing")

    def on_disconnect(self):  # type: ignore[override]
        log.warning("X stream DISCONNECTED — falling back to RSS until reconnect")

    def on_errors(self, errors):  # type: ignore[override]
        log.warning(f"X stream errors: {errors}")

    def on_request_error(self, status_code):  # type: ignore[override]
        # 429 TooManyConnections = another connection holds the single
        # allowed slot (redeploy overlap OR >1 running instance). tweepy
        # retries with backoff; this makes the cause visible in logs.
        if status_code == 429:
            log.error(
                "X stream HTTP 429 — the single allowed streaming connection "
                "is already in use. Cause: a redeploy still draining, OR the "
                "Render service is running MORE THAN ONE INSTANCE (X allows "
                "only 1 stream per token). tweepy will keep retrying with "
                "backoff; if this persists, set Render instance count to 1."
            )
        else:
            log.error(f"X stream HTTP error {status_code}")

    def on_exception(self, exception):  # type: ignore[override]
        log.error(f"X stream exception: {exception}")


def _sync_rules(client: _Listener, handles: list[str]) -> None:
    existing = client.get_rules()
    if existing and existing.data:
        client.delete_rules([r.id for r in existing.data])
    chunks = _chunk_rules(handles)
    rules = [tweepy.StreamRule(value=r, tag=f"journalists_{i}")
             for i, r in enumerate(chunks)]
    if not rules:
        return
    resp = client.add_rules(rules)
    created = len(resp.data) if getattr(resp, "data", None) else 0
    # Surface rejected rules — on the Basic API tier the filtered stream
    # caps at 5 rules, so extra chunks are rejected and their handles are
    # SILENTLY dropped from coverage. Logging it makes the cap visible.
    errors = getattr(resp, "errors", None)
    if errors:
        log.error(
            f"X stream: {len(errors)} rule(s) REJECTED — handles in those "
            f"rules are NOT being covered. Likely the per-tier rule cap "
            f"(Basic=5 rules). Errors: {errors}"
        )
    if created < len(rules):
        log.error(
            f"X stream: only {created}/{len(rules)} rules accepted. "
            f"{len(rules) - created} chunk(s) dropped — upgrade API tier or "
            f"trim the handle list to restore full coverage."
        )
    log.info(f"X stream rules synced: {created}/{len(rules)} rule(s) accepted, "
             f"{len(handles)} handles requested")


def start_stream(bearer_token: str, loop: asyncio.AbstractEventLoop,
                 queue: asyncio.Queue,
                 handles: list[str] | None = None) -> threading.Thread:
    """Start the filtered stream in a background thread. Returns the thread."""
    handles = handles or JOURNALISTS
    client = _Listener(bearer_token, loop, queue)
    _sync_rules(client, handles)
    client.filter(
        tweet_fields=["author_id", "referenced_tweets", "created_at", "attachments"],
        expansions=["author_id", "attachments.media_keys"],
        user_fields=["username", "name"],
        media_fields=["url", "preview_image_url", "type"],
        threaded=True,
    )
    # tweepy's threaded filter creates its own thread internally; we return
    # the client's thread handle for caller visibility.
    return client.thread  # type: ignore[attr-defined]


# ─── Recent-search polling (PRIMARY ingestion path) ──────────────────────────
def _build_search_queries(handles: Iterable[str], max_len: int = 460) -> list[str]:
    """OR-joined `from:` clauses for GET /2/tweets/search/recent, each short
    enough that wrapping in parens + appending `-is:retweet -is:reply` stays
    under the 512-char query cap."""
    return _chunk_rules(handles, max_len=max_len)


# Bounded global dedup of tweet IDs across BOTH poll loops so a tweet caught
# by the fast priority loop is never re-emitted by the slow full loop.
_SEEN_MAX = 8000
_seen_order: deque = deque()
_seen_set: set = set()


def _mark_seen(tweet_id: str) -> bool:
    """Return True if this is the first time we've seen the id (and record it),
    False if already seen."""
    if tweet_id in _seen_set:
        return False
    _seen_set.add(tweet_id)
    _seen_order.append(tweet_id)
    if len(_seen_order) > _SEEN_MAX:
        old = _seen_order.popleft()
        _seen_set.discard(old)
    return True


def _event_from_json(tweet: dict, users: dict, media: dict) -> Optional[dict]:
    """Build a queue event from a search/recent tweet object. None to skip."""
    # -is:retweet -is:reply is applied at the API; still drop quote-tweets.
    for ref in (tweet.get("referenced_tweets") or []):
        if ref.get("type") in ("retweeted", "replied_to", "quoted"):
            return None
    author = users.get(tweet.get("author_id"))
    if not author:
        return None
    # First attached PHOTO only (skip video/GIF preview stills).
    image_url = None
    keys = ((tweet.get("attachments") or {}).get("media_keys")) or []
    for k in keys:
        m = media.get(k)
        if m and m.get("type") == "photo" and m.get("url"):
            image_url = m["url"]
            break
    return {
        "id": str(tweet["id"]),
        "text": tweet.get("text", ""),
        "handle": author.get("username", ""),
        "author_name": author.get("name", ""),
        "created_at": tweet.get("created_at"),
        "image_url": image_url,
    }


async def poll_recent_tweets(bearer_token: str, queue: asyncio.Queue,
                             handles: list[str], interval: int,
                             label: str = "poll") -> None:
    """Poll GET /2/tweets/search/recent for `handles` every `interval`s and
    push new original tweets onto `queue`. The first pass per query only
    seeds since_id (no history dump); subsequent passes emit only new tweets.
    Never raises — logs and continues so one bad request can't kill the loop.
    """
    headers = {"Authorization": f"Bearer {bearer_token}"}
    queries = _build_search_queries(handles)
    since: dict[int, str] = {}
    seeded = False
    log.info(f"X poll[{label}] starting — {len(handles)} handles, "
             f"{len(queries)} queries, every {interval}s")
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            for i, q in enumerate(queries):
                params = {
                    "query": f"({q}) -is:retweet -is:reply",
                    "max_results": 50,
                    "tweet.fields": "author_id,created_at,attachments,referenced_tweets",
                    "expansions": "author_id,attachments.media_keys",
                    "user.fields": "username,name",
                    "media.fields": "url,preview_image_url,type",
                }
                if since.get(i):
                    params["since_id"] = since[i]
                try:
                    r = await client.get(_SEARCH_URL, headers=headers, params=params)
                except Exception as e:
                    log.warning(f"X poll[{label}] request failed: {e}")
                    continue
                if r.status_code == 429:
                    log.warning(f"X poll[{label}] rate-limited (429) — backing "
                                f"off 60s. Consider raising X_POLL_*_INTERVAL.")
                    await asyncio.sleep(60)
                    continue
                if r.status_code != 200:
                    log.warning(f"X poll[{label}] HTTP {r.status_code}: "
                                f"{r.text[:200]}")
                    continue
                try:
                    data = r.json()
                except Exception:
                    continue
                tweets = data.get("data") or []
                inc = data.get("includes") or {}
                users = {u["id"]: u for u in inc.get("users", [])}
                media = {m["media_key"]: m for m in inc.get("media", [])}
                if tweets:
                    since[i] = tweets[0]["id"]      # newest first
                if not seeded:
                    continue                         # first pass: seed only
                emitted = 0
                for t in reversed(tweets):           # oldest → newest
                    if not _mark_seen(str(t["id"])):
                        continue
                    ev = _event_from_json(t, users, media)
                    if not ev:
                        continue
                    try:
                        queue.put_nowait(ev)
                        emitted += 1
                    except asyncio.QueueFull:
                        log.warning(f"X poll[{label}] queue full — dropping tweet")
                if emitted:
                    log.info(f"X poll[{label}] queued {emitted} new tweet(s)")
            seeded = True
            await asyncio.sleep(interval)
