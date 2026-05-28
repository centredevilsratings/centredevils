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
    "brfootball", "OneFootball", "BeFootball", "eurofootcom",

    # ── Rival aggregators (track to see what they break) ──
    "TouchlineX", "DeadlineDayLive", "AlbicelesteTalk",

    # ── Club / fan aggregator accounts ──
    "MadridZone", "MadridXtra", "ManagingBarca", "ATMUniverse",
    "PSGINT_", "iMiaSanMia", "AlNassrZone", "TotalCristiano",
    "mufcMPB",

    # ── Regional / language aggregators ──
    "ActuFoot_", "vibesfoot", "ActuSPL",
]


def _chunk_rules(handles: Iterable[str], max_len: int = 480) -> list[str]:
    """Build OR-joined `from:` rules, each under the per-rule character limit."""
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

        users = {u.id: u for u in (response.includes or {}).get("users", [])}
        author = users.get(tweet.author_id)
        if not author:
            return

        event = {
            "id": str(tweet.id),
            "text": tweet.text,
            "handle": author.username,
            "author_name": author.name,
            "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
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
    rules = [tweepy.StreamRule(value=r, tag=f"journalists_{i}")
             for i, r in enumerate(_chunk_rules(handles))]
    if rules:
        client.add_rules(rules)
        log.info(f"X stream rules synced: {len(rules)} rule(s), "
                 f"{len(handles)} handles")


def start_stream(bearer_token: str, loop: asyncio.AbstractEventLoop,
                 queue: asyncio.Queue,
                 handles: list[str] | None = None) -> threading.Thread:
    """Start the filtered stream in a background thread. Returns the thread."""
    handles = handles or JOURNALISTS
    client = _Listener(bearer_token, loop, queue)
    _sync_rules(client, handles)
    client.filter(
        tweet_fields=["author_id", "referenced_tweets", "created_at"],
        expansions=["author_id"],
        user_fields=["username", "name"],
        threaded=True,
    )
    # tweepy's threaded filter creates its own thread internally; we return
    # the client's thread handle for caller visibility.
    return client.thread  # type: ignore[attr-defined]
