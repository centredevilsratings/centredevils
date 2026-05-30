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
  "label": "BREAKING" | "JUST IN" | "NEW" | "OFFICIAL" | null,
  "is_quote": <true ONLY for sensational viral-worthy quotes — see QUOTE RULES. False for all news drafts.>,
  "line1_template": "Sentence with the key fact replaced by the literal token {{KEY}}. Max ~120 chars including the placeholder. Empty string when is_quote=true.",
  "key_fact": "1-3 WORD KEY FACT in UPPERCASE ASCII letters/digits/spaces only (e.g. MUSCLE INJURY, DONE DEAL, SACKED, LEAVE, RETIRING, AGREED, SIGNED). No punctuation. Empty string when is_quote=true.",
  "emoji_flag": "Exactly one reaction emoji + one country flag emoji matching the story. Examples: 🤕🇧🇷 (injury), 👋🇺🇸 (departure), ✅🏴󠁧󠁢󠁥󠁮󠁧󠁿 (signing/England), 🏆 (trophy). Empty string only if truly unclear or is_quote=true.",
  "context": "Optional single sentence adding ONE key follow-up detail (≤ 110 chars), or null.",
  "attribution_handle": "If the source tweet credits a different journalist/outlet as the original reporter (e.g. 'via @FabrizioRomano', 'per @David_Ornstein', '🚨 @DiMarzio reports'), put that handle here WITHOUT the @ (e.g. 'FabrizioRomano'). Otherwise null — we'll attribute to the original poster. IGNORED when label='OFFICIAL'.",
  "quote_speaker": "Speaker name + brief topic context (e.g. 'Mikel Arteta after Arsenal's CL final loss', 'Pep Guardiola on Mikel Arteta'). REQUIRED when is_quote=true, else empty string.",
  "quote_text": "The verbatim quote text WITHOUT outer quotation marks — we add them. REQUIRED when is_quote=true, else empty string.",
  "story_id": "Stable kebab-case slug uniquely identifying THIS underlying story for deduplication. Format: <player-or-subject>-<club>-<action>, lowercase, max 6 words, hyphenated. MUST be identical for EVERY different framing of the same underlying event."
}

VOICE RULES:
- "BREAKING" → confirmed transfers, sackings, injuries, retirements, contract signings reported by trusted journalists / outlets.
- "JUST IN" → credible developing/JUST-reported news (not yet officially confirmed).
- "NEW" → newly surfaced reporting, fresh angles, or notable analysis that isn't quite breaking or just-in.
- "OFFICIAL" → news directly announced by the CLUB, PLAYER, or LEAGUE themselves. Triggers: club's own social account ("Real Madrid is delighted to announce…"), player's own account ("It's official, I'm joining…"), league's announcement, or a trusted outlet reporting "the club has officially confirmed". When you use OFFICIAL, the source IS the team/player/league — no journalist source line will be added. Only use OFFICIAL when the announcement clearly originates from the entity itself, not from a reporter's claim.
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

NO-FABRICATION RULE — ABSOLUTE:
- You may ONLY use facts that appear in the source text. NEVER invent fees, contract lengths, dates, ages, nationalities, decision-makers, rival clubs, medical dates, or any other detail that is not literally present in the source.
- If a fact is uncertain or paraphrased in the source ("reportedly", "claims", "according to"), hedge in your draft ("reportedly", "per reports").
- If you find yourself wanting to write something that "sounds plausible" but isn't in the source — stop and either drop that detail or set skip=true.

"HERE WE GO" RULE — STRICTEST, READ TWICE:
"HERE WE GO" is the trademarked catchphrase of Fabrizio Romano (@FabrizioRomano). You may ONLY use it as the key_fact if BOTH of these conditions hold:
1. The original source tweet was POSTED BY @FabrizioRomano (or an aggregator explicitly relaying his tweet), AND
2. The source text LITERALLY contains the phrase "here we go" (case-insensitive) or its emoji shorthand "🤝🟢".
If either condition fails, you are FORBIDDEN from using "HERE WE GO". Instead use one of: "DONE DEAL", "AGREED", "SIGNED", "CONFIRMED", "COMPLETED". Do not put words in Fabrizio's mouth. Do not let an aggregator's hype phrasing trigger it. If unsure, never "HERE WE GO".

QUOTE RULES — VIRAL-WORTHY ONLY:
Set is_quote=true ONLY when the source contains a quote that will genuinely BLOW UP the internet. Sensational, controversial, or emotionally explosive. Examples of what DOES qualify:
- Manager attacking another club / player / referee / federation
- Player revealing they want to leave, naming a preferred destination
- Dressing-room drama / locker-room split / public dispute
- Surprising admission ("I almost signed for…", "I was offered…")
- Player or manager confirming retirement / departure on the spot
- Coach criticising owners, ownership, board, or transfer policy publicly
- Player calling out the manager / teammates / fans
- Emotional moment (tears, World Cup speech, last-ever press conference)
- Conspiracy-flavoured claim ("the schedule was designed to…")
- Direct shot at a rival manager / club / pundit by name

Examples of what DOES NOT qualify — these are ALL skip=true:
- Generic post-match interview ("happy with three points", "boys did well")
- Training-ground update / fitness update / squad rotation talk
- "We'll keep fighting", "we believe in ourselves", "we'll improve"
- Thanking fans / dedicating wins / sponsor obligations
- "We need to take it one game at a time"
- Tactical platitudes without naming anyone or anything specific
- Anything that wouldn't have a football Twitter account quote-tweeting it in shock

Format when is_quote=true:
- line1_template, key_fact, emoji_flag must be empty strings.
- quote_speaker = speaker + brief topic anchor (e.g. "Pep Guardiola on Mikel Arteta", "Mikel Arteta after losing the CL final", "Vinicius Jr on the referee").
- quote_text = the verbatim quote (we add the surrounding quotation marks). Trim to the most explosive ~200 chars if it's long; do not paraphrase.
- Still set attribution_handle if an aggregator is relaying another journalist's interview.
- label can stay null for quotes (the format itself signals it's a quote).

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
- "in talks" (when no specifics given)
- "monitoring the situation"
- "weighing up a move"
- "reportedly keen on" (with no fee, no timeline)
- "tabled an offer" (with no amount)
- "preparing a bid" (with no amount or timing)
- Any sentence that, if removed, the reader loses zero information.

If the only thing you have is "Club X interested in Player Y" with no fee, no timeline, no source-named decision-maker, no contract terms — set skip=true. That is too thin.

Context sentence rules:
- Must add a DIFFERENT specific fact from line 1 (don't restate the headline).
- Acceptable contents: fee breakdown, contract length, alternative target, rival club bidding, medical date, source name (e.g. "per @FabrizioRomano"), squad-impact detail.
- If you cannot add a specific second fact, set context to null. Do not pad.

ATTRIBUTION RULES — aggregator passthrough:
The source tweet may come from an aggregator account that is RELAYING someone else's reporting. Known aggregators include: @TouchlineX, @DeadlineDayLive, @AlbicelesteTalk, @brfootball, @OneFootball, @_BeFootball, @eurofootcom, @theMadridZone, @MadridXtra, @ManagingBarca, @atletiuniverse, @PSGINT_, @iMiaSanMia, @AlNassrZone, @TotalCristiano, @mufcMPB, @ActuFoot_, @vibesfoot, @ActuSPL.
If the source tweet body credits another journalist or outlet — patterns like "via @X", "per @X", "🚨 @X reports", "(@X)", "[@X]", "source: @X", "according to @X", "@X:", "X reports" — set attribution_handle to that credited handle (without the @). That's the real reporter; the aggregator is just the loudspeaker.
If no credit is given in the body, leave attribution_handle null.

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

Source tweet by @David_Ornstein: "Manchester United have completed signing of Xavi Simons from RB Leipzig on a five-year contract. Total package worth €70m guaranteed plus €5m add-ons."
Output:
{"skip": false, "label": "BREAKING", "line1_template": "Manchester United complete the {{KEY}} of Xavi Simons from RB Leipzig.", "key_fact": "SIGNING", "emoji_flag": "✅🔴", "context": "Five-year contract. €70m guaranteed + €5m add-ons.", "attribution_handle": null, "story_id": "xavi-simons-manchester-united-transfer"}
[NOTE: not Fabrizio, so NO "HERE WE GO" — used "SIGNING" instead.]

Source tweet by @TouchlineX: "Tottenham preparing a bid for Mason Greenwood. Spurs monitoring the situation."
Output:
{"skip": true, "label": null, "is_quote": false, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null, "quote_speaker": "", "quote_text": "", "attribution_handle": null, "story_id": ""}
[NOTE: no fee, no timeline, no source-named decision-maker — all banned filler phrasing. Skip.]

Source tweet by @realmadrid: "Real Madrid C. F. is delighted to announce the appointment of José Mourinho as Head Coach until June 2029. Mourinho will be officially unveiled tomorrow at the Santiago Bernabéu."
Output:
{"skip": false, "label": "OFFICIAL", "is_quote": false, "line1_template": "Real Madrid appoint José Mourinho as Head Coach until {{KEY}}.", "key_fact": "JUNE 2029", "emoji_flag": "🤝🇵🇹", "context": "Unveiling tomorrow at the Santiago Bernabéu.", "attribution_handle": null, "quote_speaker": "", "quote_text": "", "story_id": "mourinho-real-madrid-appointment"}
[NOTE: posted by the club itself — label=OFFICIAL, no source line will be added.]

Source tweet by @SkyKaveh: "Mikel Arteta to Sky Sports after Arsenal's Champions League final defeat: 'We deserved to win this tonight. I told the boys this is not over. We will be back and we will win it next year. I guarantee it.'"
Output:
{"skip": false, "label": null, "is_quote": true, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null, "quote_speaker": "Mikel Arteta after Arsenal's Champions League final defeat", "quote_text": "We deserved to win this tonight. I told the boys this is not over. We will be back and we will win it next year. I guarantee it.", "attribution_handle": null, "story_id": "arteta-cl-final-defeat-promise"}
[NOTE: emotionally charged + bold prediction after a huge moment — qualifies. is_quote=true, no LABEL needed.]

Source tweet by @TheAthleticFC: "Pep Guardiola post-match: 'We are happy with the three points. The boys played well, especially in the second half. We have to keep going.'"
Output:
{"skip": true, "label": null, "is_quote": false, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null, "quote_speaker": "", "quote_text": "", "attribution_handle": null, "story_id": ""}
[NOTE: generic post-match platitude — exactly what we DON'T draft. Skip.]

Source tweet by @TouchlineX: "🚨 NEW: Manchester United have reached full agreement with RB Leipzig for Xavi Simons. €70m total package, 5-year deal. Medical scheduled for tomorrow. Via @FabrizioRomano 🔴"
Output:
{"skip": false, "label": "BREAKING", "line1_template": "Manchester United reach full agreement with RB Leipzig for Xavi Simons, {{KEY}}!", "key_fact": "DONE DEAL", "emoji_flag": "✅🔴", "context": "€70m total package, 5-year deal. Medical scheduled for tomorrow.", "attribution_handle": "FabrizioRomano"}

Source tweet by @theMadridZone: "🚨 Real Madrid have submitted a €60m bid for Piero Hincapié. Leverkusen want closer to €75m. Per @MatteMoretto."
Output:
{"skip": false, "label": "JUST IN", "line1_template": "Real Madrid submit {{KEY}} for Piero Hincapié, Leverkusen holding out.", "key_fact": "€60M BID", "emoji_flag": "👀🇪🇨", "context": "Leverkusen want closer to €75m for the Ecuadorian defender.", "attribution_handle": "MatteMoretto"}

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
    label = parsed.get("label")
    is_quote = bool(parsed.get("is_quote"))

    # ── Sensational-quote path ────────────────────────────────────────────
    if is_quote:
        speaker = (parsed.get("quote_speaker") or "").strip()
        quote = (parsed.get("quote_text") or "").strip().strip('"')
        if not speaker or not quote:
            return None
        prefix = "🚨🚨| "
        line1 = f"{speaker}:"
        override = (parsed.get("attribution_handle") or "").strip().lstrip("@")
        final_handle = override if override else handle
        attribution = f"[@{final_handle}]"
        draft = "\n".join([prefix + line1, f'"{quote}"', attribution])
        if len(draft) <= MAX_LEN:
            return draft
        # Trim the quote if over budget.
        budget = MAX_LEN - len(prefix + line1) - len(attribution) - 6  # newlines + quotes
        if budget > 40:
            trimmed = quote[:budget].rsplit(" ", 1)[0] + "…"
            return "\n".join([prefix + line1, f'"{trimmed}"', attribution])
        return None

    # ── News path ─────────────────────────────────────────────────────────
    template = (parsed.get("line1_template") or "").strip()
    key_fact = (parsed.get("key_fact") or "").strip()
    if not template or not key_fact or "{{KEY}}" not in template:
        return None

    emoji_flag = (parsed.get("emoji_flag") or "").strip()
    context = (parsed.get("context") or "").strip() if parsed.get("context") else ""

    key_bold = to_math_bold(key_fact.upper())
    line1 = template.replace("{{KEY}}", key_bold).strip()
    if emoji_flag:
        line1 = f"{line1} {emoji_flag}"

    prefix = "🚨🚨| "
    if label in ("BREAKING", "JUST IN", "NEW", "OFFICIAL"):
        prefix += f"{label}: "

    # OFFICIAL announcements come from the club / player / league themselves
    # — no journalist source line is added.
    attribution = ""
    if label != "OFFICIAL":
        override = (parsed.get("attribution_handle") or "").strip().lstrip("@")
        final_handle = override if override else handle
        attribution = f"[@{final_handle}]"

    parts = [prefix + line1]
    if context:
        parts.append(context)
    if attribution:
        parts.append(attribution)

    draft = "\n".join(parts)
    if len(draft) <= MAX_LEN:
        return draft

    # Over budget — drop context first.
    if context:
        parts = [prefix + line1]
        if attribution:
            parts.append(attribution)
        draft = "\n".join(parts)
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
                  source: str) -> Optional[tuple[str, str]]:
    """Draft a CentreGoals tweet from a news article. Returns (draft, story_id)."""
    handle = _source_to_handle(source)
    body = f"Headline: {title}\n\nSummary: {summary}"
    return draft_tweet(claude, body, handle, source)


def draft_tweet(claude: anthropic.Anthropic, source_text: str,
                handle: str, author_name: str = "") -> Optional[tuple[str, str]]:
    """Generate a CentreGoals draft. Returns (draft, story_id) or None."""
    user_msg = (
        f"Source tweet by @{handle}"
        + (f" ({author_name})" if author_name else "")
        + f":\n\n{source_text}\n\nDraft the CentreGoals post as JSON."
    )
    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
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

    draft = _build_draft(parsed, handle)
    if not draft:
        return None
    story_id = (parsed.get("story_id") or "").strip().lower()
    return (draft, story_id)


# ─── Discord posting ─────────────────────────────────────────────────────────
async def post_draft(client: httpx.AsyncClient, webhook_url: str,
                     draft: str, source_url: Optional[str] = None,
                     image_url: Optional[str] = None,
                     logo_url: Optional[str] = None) -> bool:
    if not webhook_url:
        return False
    content = f"```\n{draft}\n```"
    if source_url:
        content += f"\nSource: <{source_url}>"

    payload: dict = {"content": content}
    embeds = []
    if image_url:
        embeds.append({"image": {"url": image_url}, "title": "Story photo"})
    if logo_url:
        embeds.append({"image": {"url": logo_url}, "title": "Logo"})
    if embeds:
        payload["embeds"] = embeds

    try:
        resp = await client.post(webhook_url, json=payload, timeout=10)
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
                         dedup_check=None,
                         dedup_record=None) -> None:
    """Long-running task: pull tweets off the stream queue and draft them."""
    while True:
        event = await queue.get()
        try:
            result = await asyncio.to_thread(
                draft_tweet, claude,
                event["text"], event["handle"], event.get("author_name", ""),
            )
            if not result:
                continue
            draft, story_id = result
            if dedup_check and dedup_check(story_id):
                log.info(f"DUP draft skipped ({story_id}): {draft[:60]}")
                continue
            source_url = f"https://twitter.com/{event['handle']}/status/{event['id']}"
            ok = await post_draft(http, webhook_url, draft, source_url,
                                  image_url=event.get("image_url"))
            if ok:
                if dedup_record:
                    dedup_record(story_id)
                log.info(f"Drafted from @{event['handle']}: {draft[:60]}")
        except Exception as e:
            log.error(f"Drafter consumer error: {e}", exc_info=True)
        finally:
            queue.task_done()
