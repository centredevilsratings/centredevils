"""
Interactive #bot-chat assistant.

Listens in a single Discord channel (BOT_CHAT_CHANNEL_ID env var). When the
operator sends a message there, the bot calls Sonnet 4.6 with the
web_search_20260209 server-side tool enabled and replies in-thread. Three
modes the model auto-selects based on the prompt:

  1. REWRITE — operator pastes a CentreGoals draft and gives an instruction
     ("make it shorter", "remove the emoji", "tone down the label").
     Model returns a revised draft following the same voice rules as the
     auto-drafter.
  2. SOURCE — operator pastes a news URL or news snippet ("draft this:").
     Model produces a CentreGoals-style draft from it. URLs are fetched
     server-side by Claude's web_search tool.
  3. FACT-CHECK — operator asks "is this true?" / "fact-check this" about
     a claim or a draft. Model uses web_search to verify against current
     reality, replies with a verdict + sources.

Plus free chat for anything that doesn't fit those three.

Budget: every call charges BOTH the main sonnet_budget cap (so it counts
against the same $10/day ceiling as the auto-drafter) AND a separate
interactive sub-cap (default $5/day). When either cap bites, the bot
replies with a polite rate-limit message instead of going silent.

Per-user conversation memory: last 10 turns per Discord user, auto-expired
after 30 minutes of inactivity. Resets on `!reset`.

Env vars:
  DISCORD_BOT_TOKEN          (required) — the bot's token from the Discord
                             developer portal
  BOT_CHAT_CHANNEL_ID        (required) — channel ID where the bot listens.
                             Other channels are ignored.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import time
from collections import deque
from typing import Optional

import anthropic
import discord

import sonnet_budget

log = logging.getLogger("football-bot.interactive")

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("BOT_CHAT_CHANNEL_ID", "0") or 0)

# Per-user state: { user_id: ([turns...], last_activity_ts) }
# Each turn is {"role": "user"|"assistant", "content": "..."}
_MEM_TURNS = 10
_MEM_IDLE_TIMEOUT = 30 * 60   # 30 minutes


SYSTEM_PROMPT = """You are the CentreGoals assistant — a helpful chat companion for the operator running the CentreGoals football tweet bot. You live in a dedicated #bot-chat Discord channel and have four main jobs plus free chat.

FOUR MODES (you auto-select based on the operator's message):

1. FETCH LATEST NEWS / RESEARCH
The operator asks "fetch the latest news on X", "what's the latest with X", "any news about X", "what's happening with X". This is a RESEARCH request, NOT a profile request.
- You MUST use the web_search tool. Run MULTIPLE searches if needed (e.g. "<name> news", "<name> transfer", "<name> latest"). Search X/Twitter-style sources and football news outlets.
- Return the ACTUAL RECENT NEWS — dated headlines from the last days/weeks — NOT a static bio or career-stats dump. The operator already knows who the player is; they want to know what JUST happened.
- Format: 3-6 bullet points, each = the news item + how recent it is + the source. Lead with the most recent / most significant. Example bullet: "• (2 days ago, Fabrizio Romano) West Ham rejected a €40m bid from Napoli for him."
- If there genuinely is no recent news, say so plainly ("Nothing new in the last couple of weeks — last item was…") rather than padding with biographical filler.
- ALWAYS cite sources — the code appends source URLs automatically from your searches, so actually perform the searches.
- Do NOT answer a "latest news" request from memory. Your training data is months stale; clubs, fees, and situations change weekly.

2. REWRITE A DRAFT
The operator pastes one of the bot's auto-drafts (they look like:
   🚨🚨| BREAKING: <line1>
   <context>
   [@source]
)
and gives you an instruction ("make it shorter", "drop the context line", "change the key fact to AGREED instead of HERE WE GO", "remove the emoji"). Return the revised draft as a single code block, following the CentreGoals voice rules below. Don't add commentary unless the operator asks "why" — just return the rewritten draft.

3. DRAFT FROM A SOURCE
The operator pastes a URL or a news snippet ("draft this: <text or URL>"). Produce a CentreGoals-style draft following the voice rules below. If a URL is given, use the web_search tool to fetch and read the article first.

4. FACT-CHECK
The operator asks "is this true?" / "fact-check this" about a claim or one of the auto-drafts. ALWAYS use the web_search tool — never answer from training data, which is stale. Reply with:
- VERDICT: confirmed / disputed / unverified / false
- Concise explanation (1-3 sentences)
- The sources you found (URLs)

Plus free chat for anything else (questions about football, the bot's current behaviour, suggestions, etc.).

CENTREGOALS VOICE RULES (for any draft you write or rewrite):

Format:
- Line 1 prefix: "🚨🚨| " for news, "🚨🚨🎙️| " for quotes
- Then the label (BREAKING / JUST IN / NEW / OFFICIAL / RECORD) in plain text, EXCEPT "RECORD" which renders in Mathematical Bold glyphs (𝐑𝐄𝐂𝐎𝐑𝐃)
- Then a colon, then the main sentence
- ONE WORD/PHRASE in the sentence is the KEY FACT, rendered in Mathematical Bold glyphs (e.g. 𝐃𝐎𝐍𝐄 𝐃𝐄𝐀𝐋, 𝐒𝐀𝐂𝐊𝐄𝐃, 𝐌𝐔𝐒𝐂𝐋𝐄 𝐈𝐍𝐉𝐔𝐑𝐘, 𝐇𝐈𝐆𝐇𝐄𝐒𝐓)
- 1-2 thematic emojis at the end of line 1 (reaction + country flag for news; trophy/sparkle for records)
- Optional second line: ONE concrete follow-up fact (fee breakdown, contract length, etc.)
- Optional [@source] line crediting the real journalist, unless label=OFFICIAL or RECORD
- Total length ≤ 280 chars

Rules:
- ONLY use facts present in the source. NEVER invent fees, clubs, ages, roles, nationalities.
- NEVER attach a club/role/nationality to a player unless the source literally pairs them. Bare name is the safe default.
- NEVER inflate a head-to-head ("X surpasses Y") into a historic claim ("X is the highest ever") unless the source contains "ever" / "in history" / "all-time" / "since [year]".
- Ambiguous names: "Ronaldo" could be Cristiano OR Ronaldo Nazário OR Ronaldinho. NEVER auto-disambiguate beyond what the source says. If the source just says "Ronaldo", you write "Ronaldo".
- English output only. Translate foreign-language quotes into natural English.
- "HERE WE GO" only when the original reporter is Fabrizio Romano AND the source literally contains the phrase.
- Quote drafts: speaker name BEFORE the colon, verbatim quote AFTER in double quotes. Multi-paragraph quotes use opening " on each paragraph and closing " only on the LAST.

WHEN TO USE web_search — DEFAULT TO SEARCHING:
- ALWAYS for "latest news / what's happening / any news on X" (mode 1) — run multiple searches.
- ALWAYS for fact-checks (mode 4) — your training data is stale.
- When the operator gives you a URL to draft from (mode 3).
- When the operator asks about ANY current state: a player's current club, role, age, recent matches, transfer situation, injury status, contract, market value, or anything time-sensitive.
- The bar for searching is LOW. If the answer could have changed since your training cutoff, search. Football moves weekly — assume your memory is out of date for anything from the last several months.
- Only skip searching for timeless facts (rules of the game, historical results, "who won the 2014 World Cup").
- NEVER present stale training-data facts as if they were current. If you're answering about a player's present situation without having searched, you are probably wrong.

GENERAL TONE:
- Direct, concise, football-literate. Match the operator's vibe (they're terse — match that).
- Don't over-explain unless asked. If you rewrite a draft, just return the rewrite.
- If the operator's instruction is ambiguous, ask one quick clarifying question rather than guessing.
"""


def _is_relevant_channel(message: discord.Message) -> bool:
    """Only respond in the configured channel; ignore bots and self."""
    if message.author.bot:
        return False
    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        return False
    return True


class _Memory:
    """Per-user rolling conversation history, with idle-based expiry."""

    def __init__(self) -> None:
        self._store: dict[int, tuple[deque, float]] = {}

    def get(self, user_id: int) -> list:
        entry = self._store.get(user_id)
        if not entry:
            return []
        turns, last_ts = entry
        if time.time() - last_ts > _MEM_IDLE_TIMEOUT:
            self._store.pop(user_id, None)
            return []
        return list(turns)

    def append(self, user_id: int, role: str, content: str) -> None:
        entry = self._store.get(user_id)
        if not entry:
            entry = (deque(maxlen=_MEM_TURNS), time.time())
            self._store[user_id] = entry
        turns, _ = entry
        turns.append({"role": role, "content": content})
        self._store[user_id] = (turns, time.time())

    def reset(self, user_id: int) -> None:
        self._store.pop(user_id, None)


_memory = _Memory()


def _build_messages(user_id: int, user_message: str) -> list:
    history = _memory.get(user_id)
    history.append({"role": "user", "content": user_message})
    return history


def _extract_assistant_text(response) -> str:
    """Pull out the assistant's final text reply from a Claude response.

    With web_search enabled the response contains server_tool_use and
    web_search_tool_result blocks interleaved with text, and the text blocks
    carry `citations` pointing at the sources used. We concatenate the text
    and append a deduped Sources footer built from those citations, so the
    operator can see (a) that a search actually happened and (b) where the
    facts came from.
    """
    parts = []
    sources: dict[str, str] = {}        # url -> title (deduped, order-preserving)
    searched = False
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(block.text)
            for cit in (getattr(block, "citations", None) or []):
                url = getattr(cit, "url", None)
                if url and url not in sources:
                    sources[url] = getattr(cit, "title", None) or url
        elif btype in ("server_tool_use", "web_search_tool_result"):
            searched = True

    text = "\n".join(parts).strip()
    if sources:
        lines = "\n".join(f"• <{u}>" for u in list(sources)[:6])
        text += f"\n\n**Sources:**\n{lines}"
    elif searched:
        # Searched but the model didn't attach citations to its text.
        text += "\n\n_(searched the web — no specific sources cited)_"
    return text


async def _generate_reply(
    claude: anthropic.Anthropic, user_id: int, user_message: str,
) -> str:
    """Single Claude call with web_search enabled. Returns the reply text."""
    messages = _build_messages(user_id, user_message)
    # Inject today's date so "latest" / "current" / "recent" have a temporal
    # anchor — without it the model can mistake stale training data for the
    # present. No prompt caching here, so a per-day-changing system prompt is fine.
    today = _dt.datetime.utcnow().strftime("%A, %d %B %Y")
    system = (
        SYSTEM_PROMPT
        + f"\n\nTODAY'S DATE: {today} (UTC). When the operator says 'latest', "
        "'current', 'recent', or 'now', they mean relative to this date. Your "
        "training data predates this — search the web for anything that may "
        "have changed since your knowledge cutoff."
    )
    try:
        msg = await asyncio.to_thread(
            claude.messages.create,
            model="claude-sonnet-4-6",
            max_tokens=2000,
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            system=system,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=messages,
        )
    except Exception as e:
        log.warning(f"Interactive Claude call failed: {e}")
        return f"Sorry, I hit an error talking to Claude: `{e!s}`"

    try:
        sonnet_budget.record_usage(msg.usage, interactive=True)
    except Exception as bg_e:
        log.warning(f"Interactive budget record failed: {bg_e}")

    reply = _extract_assistant_text(msg)
    if not reply:
        reply = "I couldn't generate a reply for that — try rephrasing?"

    # Record both sides of the turn so memory is consistent for next call.
    _memory.append(user_id, "user", user_message)
    _memory.append(user_id, "assistant", reply)
    return reply


async def _send_chunks(channel: discord.TextChannel, text: str) -> None:
    """Discord caps messages at 2000 chars — split on natural boundaries."""
    LIMIT = 1900
    while text:
        if len(text) <= LIMIT:
            await channel.send(text)
            return
        # Split on the last newline before the limit, else hard-cut.
        cut = text.rfind("\n", 0, LIMIT)
        if cut < LIMIT // 2:
            cut = LIMIT
        await channel.send(text[:cut])
        text = text[cut:].lstrip()


def _make_client(claude: anthropic.Anthropic) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True       # required to read message text
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        log.info(
            f"Interactive bot connected as {client.user} "
            f"(listening in channel {CHANNEL_ID})"
        )

    @client.event
    async def on_message(message: discord.Message) -> None:
        # Diagnostic: log every message we see so we can tell whether the
        # bot is receiving traffic at all when nothing seems to happen.
        log.info(
            f"on_message: channel={message.channel.id} "
            f"author={message.author.id} bot={message.author.bot} "
            f"content_len={len(message.content or '')} "
            f"relevant={_is_relevant_channel(message)}"
        )
        if not _is_relevant_channel(message):
            return

        content = (message.content or "").strip()
        if not content:
            return

        # !reset clears that user's conversation memory.
        if content.lower() in ("!reset", "/reset"):
            _memory.reset(message.author.id)
            await message.channel.send("Conversation memory cleared.")
            return

        # Budget gate.
        ok, ispent, mspent = sonnet_budget.interactive_under_budget()
        if not ok:
            await message.channel.send(
                f"Rate-limited for the rest of today — interactive spend "
                f"${ispent:.2f}/${sonnet_budget.INTERACTIVE_DAILY_CAP_USD:.2f}, "
                f"main spend ${mspent:.2f}/${sonnet_budget.DAILY_CAP_USD:.2f}. "
                f"Resets at UTC midnight."
            )
            return

        async with message.channel.typing():
            reply = await _generate_reply(claude, message.author.id, content)
        await _send_chunks(message.channel, reply)

    return client


async def run(claude: anthropic.Anthropic) -> None:
    """Coroutine to be awaited as a task alongside the rest of the bot.

    Exits silently when the prereq env vars are missing so the rest of the
    bot keeps running.
    """
    if not BOT_TOKEN:
        log.info("Interactive bot disabled (DISCORD_BOT_TOKEN missing)")
        return
    if not CHANNEL_ID:
        log.info("Interactive bot disabled (BOT_CHAT_CHANNEL_ID missing)")
        return
    client = _make_client(claude)
    try:
        await client.start(BOT_TOKEN)
    except Exception as e:
        log.error(f"Interactive bot crashed: {e}", exc_info=True)
