"""
X (Twitter) filtered stream listener for football journalists.
Pushes incoming tweets onto an asyncio.Queue for downstream processing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Iterable

import tweepy

log = logging.getLogger("football-bot.x_stream")


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

    def on_errors(self, errors):  # type: ignore[override]
        log.warning(f"X stream errors: {errors}")

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
