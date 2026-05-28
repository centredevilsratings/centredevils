"""
CentreGoals tweet drafter.
Turns a journalist's tweet into a pre-written draft in CentreGoals voice
and posts it to a Discord webhook for human review.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

import anthropic
import httpx

log = logging.getLogger("football-bot.tweet_drafter")


# ─── Math-bold helper ────────────────────────────────────────────────────────
_UPPER_OFFSET = 0x1D400 - ord('A')
_LOWER_OFFSET = 0x1D41A - ord('a')
_DIGIT_OFFSET = 0x1D7CE - ord('0')


def to_math_bold(s: str) -> str:
    """Convert ASCII A-Z/a-z/0-9 to Mathematical Bold unicode."""
    out: list[str] = []
    for c in s:
        cp = ord(c)
        if 0x41 <= cp <= 0x5A:
            out.append(chr(cp + _UPPER_OFFSET))
        elif 0x61 <= cp <= 0x7A:
            out.append(chr(cp + _LOWER_OFFSET))
        elif 0x30 <= cp <= 0x39:
            out.append(chr(cp + _DIGIT_OFFSET))
        else:
            out.append(c)
    return "".join(out)


# ─── Prompting ───────────────────────────────────────────────────────────────
DRAFTER_SYSTEM = """You draft CentreGoals tweets — concise, breaking-style \
football news posts. Given a source tweet from a journalist or outlet, return \
ONLY valid JSON with this schema:

{
  "skip": <true if the source is not actionable football news (e.g. opinion, \
banter, podcast plug, off-topic) — otherwise false>,
  "label": "BREAKING" | "JUST IN" | null,
  "line1_template": "Sentence with the key fact replaced by the literal token {{KEY}}. Max ~120 chars including the {{KEY}} placeholder.",
  "key_fact": "1-3 WORD KEY FACT IN UPPERCASE ASCII (e.g. MUSCLE INJURY, DONE DEAL, SACKED, LEAVE, RETIRING). Letters/digits/spaces only.",
  "emoji_flag": "One emoji + one country flag emoji that match the story (e.g. 🤕🇧🇷, 👋🇺🇸, ✅🏴󠁧󠁢󠁥󠁮󠁧󠁿). Empty string if unclear.",
  "context": "Optional 1-sentence context paragraph (<=110 chars) or null."
}

Rules:
- Use "BREAKING" for confirmed transfers, sackings, injuries, retirements.
- Use "JUST IN" for credible developing news.
- Use null label for softer/secondary stories.
- key_fact must be the most operationally important phrase (transfer status, \
injury type, decision). Never include punctuation.
- Do NOT add hashtags. Do NOT wrap the JSON in markdown. Output raw JSON only.
- If the source tweet is a retweet, reply, link-only post, or not about \
football operations, set skip=true."""


MAX_LEN = 240


def _build_draft(parsed: dict, handle: str) -> Optional[str]:
    """Assemble the final draft text from Claude's structured output."""
    template = (parsed.get("line1_template") or "").strip()
    key_fact = (parsed.get("key_fact") or "").strip()
    if not template or not key_fact or "{{KEY}}" not in template:
        return None

    label = parsed.get("label")
    emoji_flag = (parsed.get("emoji_flag") or "").strip()
    context = (parsed.get("context") or "").strip() if parsed.get("context") else ""

    key_bold = to_math_bold(key_fact.upper())
    line1 = template.replace("{{KEY}}", key_bold).strip()
    if emoji_flag:
        line1 = f"{line1} {emoji_flag}"

    prefix = "🚨🚨| "
    if label in ("BREAKING", "JUST IN"):
        prefix += f"{label}: "

    attribution = f"[@{handle}]"

    parts = [prefix + line1]
    if context:
        parts.append(context)
    parts.append(attribution)
    draft = "\n".join(parts)

    if len(draft) <= MAX_LEN:
        return draft

    # Over budget — drop context first.
    if context:
        draft = "\n".join([prefix + line1, attribution])
    if len(draft) <= MAX_LEN:
        return draft

    return None


def _parse_json(raw: str) -> Optional[dict]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception as e:
        log.debug(f"Drafter JSON parse failed: {e} | raw={raw[:200]}")
        return None


def draft_tweet(claude: anthropic.Anthropic, source_text: str,
                handle: str, author_name: str = "") -> Optional[str]:
    """Generate a CentreGoals draft. Returns None to skip."""
    user_msg = (
        f"Source tweet by @{handle}"
        + (f" ({author_name})" if author_name else "")
        + f":\n\n{source_text}\n\nDraft the CentreGoals post as JSON."
    )
    try:
        msg = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            system=DRAFTER_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = msg.content[0].text
    except Exception as e:
        log.warning(f"Claude drafter call failed: {e}")
        return None

    parsed = _parse_json(raw)
    if not parsed or parsed.get("skip"):
        return None

    return _build_draft(parsed, handle)


# ─── Discord posting ─────────────────────────────────────────────────────────
async def post_draft(client: httpx.AsyncClient, webhook_url: str,
                     draft: str, source_url: Optional[str] = None) -> bool:
    if not webhook_url:
        return False
    content = f"```\n{draft}\n```"
    if source_url:
        content += f"\nSource: <{source_url}>"
    try:
        resp = await client.post(webhook_url, json={"content": content}, timeout=10)
        if resp.status_code in (200, 204):
            return True
        log.error(f"Drafts webhook error {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Drafts webhook post failed: {e}")
        return False


# ─── Stream consumer ─────────────────────────────────────────────────────────
async def consume_stream(queue: asyncio.Queue,
                         claude: anthropic.Anthropic,
                         http: httpx.AsyncClient,
                         webhook_url: str,
                         hourly_cap_check=None,
                         hourly_cap_record=None) -> None:
    """Long-running task: pull tweets off the stream queue and draft them."""
    while True:
        event = await queue.get()
        try:
            if hourly_cap_check and not hourly_cap_check("tweet_drafts"):
                log.debug("Hourly cap hit for tweet_drafts — skipping")
                continue
            draft = await asyncio.to_thread(
                draft_tweet, claude,
                event["text"], event["handle"], event.get("author_name", ""),
            )
            if not draft:
                continue
            source_url = f"https://twitter.com/{event['handle']}/status/{event['id']}"
            ok = await post_draft(http, webhook_url, draft, source_url)
            if ok and hourly_cap_record:
                hourly_cap_record("tweet_drafts")
                log.info(f"Drafted from @{event['handle']}: {draft[:60]}")
        except Exception as e:
            log.error(f"Drafter consumer error: {e}", exc_info=True)
        finally:
            queue.task_done()
