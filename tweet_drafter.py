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
DRAFTER_SYSTEM = """You draft CentreGoals tweets — concise, breaking-style football news posts in the EXACT CentreGoals voice. Given a source tweet from a journalist or outlet (or a news headline + summary), return ONLY valid JSON with this schema:

{
  "skip": <true if the source is not actionable football news (opinion, banter, podcast/video plug, link-only post, retweet, reply, off-topic, multi-item list/ranking) — otherwise false>,
  "label": "BREAKING" | "JUST IN" | "NEW" | null,
  "line1_template": "Sentence with the key fact replaced by the literal token {{KEY}}. Max ~120 chars including the placeholder.",
  "key_fact": "1-3 WORD KEY FACT in UPPERCASE ASCII letters/digits/spaces only (e.g. MUSCLE INJURY, DONE DEAL, SACKED, LEAVE, RETIRING, HERE WE GO, AGREED, SIGNED). No punctuation.",
  "emoji_flag": "Exactly one reaction emoji + one country flag emoji matching the story. Examples: 🤕🇧🇷 (injury), 👋🇺🇸 (departure), ✅🏴󠁧󠁢󠁥󠁮󠁧󠁿 (signing/England), 🏆 (trophy). Empty string only if truly unclear.",
  "context": "Optional single sentence adding ONE key follow-up detail (≤ 110 chars), or null."
}

VOICE RULES:
- "BREAKING" → confirmed transfers, sackings, injuries, retirements, contract signings.
- "JUST IN" → credible developing/JUST-reported news (not yet officially confirmed).
- "NEW" → newly surfaced reporting, fresh angles, or notable analysis that isn't quite breaking or just-in.
- null label → softer secondary stories.
- key_fact must be the SINGLE most operationally important phrase. Keep it 1-3 words.
- Common key_fact values: MUSCLE INJURY, ACL INJURY, DONE DEAL, HERE WE GO, AGREED, SIGNED, LEAVE, SACKED, RETIRING, EXTENDS, REJECTED, RECALLED, EYEING, LINKED.
- NO hashtags. NO markdown around the JSON. NO emojis except those in emoji_flag.
- skip=true examples: retweets, replies, link-only "see thread below" posts, ranked top-10 lists, opinion takes, podcast/video plugs, off-topic personal posts.

RECENCY RULES — CRITICAL, READ CAREFULLY:
- ONLY use "BREAKING" or "JUST IN" if the source EXPLICITLY indicates the event happened today / in the last few hours / "moments ago" / "just now" / "this morning". Look for those exact recency anchors.
- If the source references "last year", "in 2024", "last summer", "last season", "previously", "had submitted", "back in [month]", or ANY date older than ~7 days, set skip=true. Do NOT draft stale news as if it were new — even if the headline is written in present tense (many outlets clickbait this way).
- If you cannot find a clear recency anchor in the source text, do not use BREAKING or JUST IN. Downgrade to "NEW" or null label and hedge the wording ("reportedly", "according to reports").
- A bid/transfer/injury described in past tense without a date is a red flag for recycled news — prefer skip=true over guessing.

SPECIFICITY RULES — CRITICAL:
The whole point of CentreGoals is to be the FASTEST, MOST DENSE source of football facts. Generic statements waste the reader's time. Every draft must carry at least one concrete data point from the source. If the source has no concrete data, prefer skip=true over a vague draft.

REQUIRED — at least ONE of these must appear in line1_template or context:
- Transfer fee in €/£/$ (e.g. "€60m", "£25m + £5m add-ons")
- Contract length / expiry year (e.g. "5-year deal", "until 2029")
- Loan terms (e.g. "loan with €25m option to buy")
- Injury type + duration (e.g. "ACL — out 6 months", "hamstring — 3 weeks")
- Specific medical / signing date (e.g. "medical Monday", "signs Tuesday")
- Salary / wage figure
- Release clause amount
- Concrete clubs involved (e.g. "from Bayer Leverkusen", "from Sporting")
- Specific scoreline / match impact (e.g. "will miss CL final")
- Named decision-makers ("Berta pushing", "INEOS approved")

BANNED filler phrases — if your draft would contain anything like these, rewrite or skip:
- "signals their intent"
- "in the transfer market"
- "could be set for a move"
- "interest in the player"
- "as they look to strengthen"
- "ahead of the new season"
- "the Argentine/English/etc striker" (when no other info)
- "the Gunners' / Reds' / Blues' initial offer" (when nothing follows)
- Any sentence that, if removed, the reader loses zero information.

If the only thing you have is "Club X interested in Player Y" with no fee, no timeline, no source-named decision-maker, no contract terms — set skip=true. That is too thin.

Context sentence rules:
- Must add a DIFFERENT specific fact from line 1 (don't restate the headline).
- Acceptable contents: fee breakdown, contract length, alternative target, rival club bidding, medical date, source name (e.g. "per @FabrizioRomano"), squad-impact detail.
- If you cannot add a specific second fact, set context to null. Do not pad.

FEW-SHOT EXAMPLES (study these — these are the EXACT voice to match):

Source: "🚨 Neymar suffered a muscle injury during training today. Brazilian FA confirms he'll be out 2-3 weeks after scans. Will miss pre-WC friendlies and possibly the Morocco opener."
Output:
{"skip": false, "label": "BREAKING", "line1_template": "Neymar has suffered a {{KEY}} and will be out for 2-3 weeks after tests confirmed the issue.", "key_fact": "MUSCLE INJURY", "emoji_flag": "🤕🇧🇷", "context": "He will miss Brazil's pre-World Cup friendlies and could also miss the opening game against Morocco."}

Source: "Modrić could retire after the World Cup, Croatian veteran considering hanging up his boots after Qatar."
Output:
{"skip": false, "label": "BREAKING", "line1_template": "Luka Modrić {{KEY}} after the World Cup is a possibility.", "key_fact": "RETIRING", "emoji_flag": "👋🇭🇷", "context": null}

Source: "Pochettino set to leave USA Men's National Team job after the World Cup. AC Milan have been offered the Argentine coach by an intermediary in the last hours."
Output:
{"skip": false, "label": "JUST IN", "line1_template": "Mauricio Pochettino will {{KEY}} USA National Team after the World Cup.", "key_fact": "LEAVE", "emoji_flag": "👋🇺🇸", "context": "He has been offered to AC Milan by an intermediary in the last hours."}

Source: "🚨 Marcus Rashford to Barcelona, here we go! Loan deal until end of season with €25m option to buy and €5m loan fee. Medical scheduled for Monday in Barcelona."
Output:
{"skip": false, "label": "BREAKING", "line1_template": "Marcus Rashford to Barcelona, {{KEY}}!", "key_fact": "HERE WE GO", "emoji_flag": "✅🏴󠁬󠁧󠁢󠁥󠁮󠁧󠁿", "context": "Loan with €5m fee + €25m option to buy. Medical Monday in Barcelona."}

Source: "Headline: Real Madrid eyeing Hincapié move ahead of Arsenal. Summary: Real Madrid have made initial contact about Leverkusen's Piero Hincapié, with release clause set at €60m and Ecuador defender keen on the move. Florentino Pérez pushing the deal."
Output:
{"skip": false, "label": "NEW", "line1_template": "Real Madrid contact Leverkusen over Piero Hincapié, {{KEY}} of €60m.", "key_fact": "RELEASE CLAUSE", "emoji_flag": "👀🇪🇨", "context": "Defender keen on switch. Florentino Pérez pushing — Arsenal also tracking."}

Source: "Headline: Chelsea showing interest in Hincapie. Summary: Chelsea are reportedly keen on Bayer Leverkusen's Piero Hincapie as they look to strengthen at the back ahead of the new season."
Output:
{"skip": true, "label": null, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null}

Source: "Just listened to the new pod with the boys, hilarious stuff on Mourinho's return. Link below 👇"
Output:
{"skip": true, "label": null, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null}

Source: "Top 10 strikers in Europe right now: 1. Haaland 2. Kane 3. Mbappé 4. Lautaro 5. Vlahović 6. Osimhen 7. Lewandowski 8. Núñez 9. Isak 10. Álvarez"
Output:
{"skip": true, "label": null, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null}
"""


MAX_LEN = 280


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
    if label in ("BREAKING", "JUST IN", "NEW"):
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


# Map article source names to plausible X handles for attribution.
_SOURCE_HANDLE_MAP = {
    "BBC Sport Football": "BBCSport",
    "BBC Sport": "BBCSport",
    "Guardian Football": "guardian_sport",
    "Sky Sports Football": "SkySportsNews",
    "Sky Sports": "SkySportsNews",
    "Independent Football": "Independent",
    "ESPN FC": "ESPNFC",
    "ESPN": "ESPNFC",
    "Telegraph Football": "TeleFootball",
    "Manchester Evening News": "ManUtdMEN",
    "L'Équipe": "lequipe",
    "RMC Sport": "RMCsport",
    "Marca (EN)": "marca",
    "Get Spanish Football News": "GFFN_Spain",
    "Get Italian Football News": "GFFN_Italy",
    "Get German Football News": "GFFN_Germany",
}


def _source_to_handle(source: str) -> str:
    if source in _SOURCE_HANDLE_MAP:
        return _SOURCE_HANDLE_MAP[source]
    cleaned = source.replace("Football", "").replace("News", "").strip()
    slug = "".join(c for c in cleaned if c.isalnum())[:20]
    return slug or "Source"


def draft_article(claude: anthropic.Anthropic, title: str, summary: str,
                  source: str) -> Optional[str]:
    """Draft a CentreGoals tweet from a news article (title + summary)."""
    handle = _source_to_handle(source)
    body = f"Headline: {title}\n\nSummary: {summary}"
    return draft_tweet(claude, body, handle, source)


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
            model="claude-haiku-4-5-20251001",
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
