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

import imago

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


# ─── Aggregator + branded-graphic gating ─────────────────────────────────────
# Aggregators are pure relay accounts — they post other journalists'
# reporting without doing their own. When the source handle is one of
# these we drop BOTH the [@source] line (unless the body credits a real
# reporter that the model surfaces as attribution_handle) AND the
# attached image (their visuals are branded watermarked cards).
AGGREGATOR_HANDLES: frozenset[str] = frozenset(h.lower() for h in {
    "TouchlineX", "DeadlineDayLive", "AlbicelesteTalk",
    "brfootball", "OneFootball", "_BeFootball", "eurofootcom",
    "theMadridZone", "MadridXtra", "ManagingBarca", "atletiuniverse",
    "PSGINT_", "iMiaSanMia", "AlNassrZone", "TotalCristiano", "mufcMPB",
    "ActuFoot_", "vibesfoot", "ActuSPL",
    "ManUtdMen",  # United-focused beat feed; nearly always relays other reporters
})

# Branded-graphic posters: real journalists / outlets whose reporting is
# legit (they keep the [@source] byline) but who attach custom designed
# "BREAKING / DONE DEAL" graphics to their tweets instead of clean press
# photos. We strip the image so the operator picks one manually.
# This is a SUPERSET of AGGREGATOR_HANDLES.
BRANDED_GRAPHIC_HANDLES: frozenset[str] = AGGREGATOR_HANDLES | frozenset({
    "plettigoal",   # Florian Plettenberg — tier-1 reporter, but every
                    # tweet is a branded "DONE DEAL" card.
})


def is_aggregator(handle: str) -> bool:
    return (handle or "").lower().lstrip("@") in AGGREGATOR_HANDLES


def posts_branded_graphics(handle: str) -> bool:
    return (handle or "").lower().lstrip("@") in BRANDED_GRAPHIC_HANDLES


# ─── Women's-football filter ─────────────────────────────────────────────────
# CentreGoals covers men's football only. Pre-filter at the source-text level
# so we skip the Haiku call entirely on unambiguous women's-football tweets.
_WOMENS_FOOTBALL_RE = re.compile(
    r"\b("
    r"women(?:['’]s|s)?|"                       # women, women's, womens
    r"ladies|"
    r"lioness(?:es)?|matildas|banyana|reggae\s+girlz|"
    r"wsl|nwsl|uwcl|wucl|uswnt|wwc|"
    r"liga[\s-]*f|"
    r"frauen[-\s]?bundesliga|frauen[-\s]national|"
    r"premi[èe]re\s+ligue\s+f[ée]minine|"
    r"serie\s+a\s+femminile|"
    r"f[ée]minin[ae]?s?|"
    r"ballon\s+d['’]or\s+f[ée]minin"
    r")\b",
    re.IGNORECASE,
)


def is_womens_football(text: str) -> bool:
    return bool(_WOMENS_FOOTBALL_RE.search(text or ""))


# ─── Prompting ───────────────────────────────────────────────────────────────
DRAFTER_SYSTEM = """You draft CentreGoals tweets — concise, breaking-style football news posts in the EXACT CentreGoals voice. Given a source tweet from a journalist or outlet (or a news headline + summary), return ONLY valid JSON with this schema:

{
  "skip": <true if the source is not actionable football news (opinion, banter, podcast/video plug, link-only post, retweet, reply, off-topic, multi-item list/ranking) — otherwise false>,
  "label": "BREAKING" | "JUST IN" | "NEW" | "OFFICIAL" | "RECORD" | null,
  "is_quote": <true ONLY for sensational viral-worthy quotes — see QUOTE RULES. False for all news drafts.>,
  "line1_template": "Sentence with the key fact replaced by the literal token {{KEY}}. Max ~120 chars including the placeholder. Empty string when is_quote=true.",
  "key_fact": "1-3 WORD KEY FACT in UPPERCASE ASCII letters/digits/spaces only (e.g. MUSCLE INJURY, DONE DEAL, SACKED, LEAVE, RETIRING, AGREED, SIGNED). No punctuation. Empty string when is_quote=true.",
  "emoji_flag": "1-2 thematic emojis at the end of line 1. For news: typically one reaction emoji + one country flag (🤕🇧🇷 injury, 👋🇺🇸 departure, ✅🏴󠁧󠁢󠁥󠁮󠁧󠁿 signing/England). For RECORD: trophy/sparkle/medal combos work too (🏆🇪🇸, 🌟🇪🇸, 😱❌ for never-achieved). Empty string only if truly unclear or is_quote=true.",
  "context": "Optional single sentence adding ONE key follow-up detail (≤ 110 chars), or null.",
  "attribution_handle": "If the source tweet credits a different journalist/outlet as the original reporter (e.g. 'via @FabrizioRomano', 'per @David_Ornstein', '🚨 @DiMarzio reports'), put that handle here WITHOUT the @ (e.g. 'FabrizioRomano'). Otherwise null — we'll attribute to the original poster. IGNORED when label='OFFICIAL'.",
  "quote_speaker": "Speaker name, optionally followed by 'on <topic>' (e.g. 'Mikel Arteta on penalty selection', 'Nasser Al-Khelaifi', 'Vinicius Jr on the referee'). No trailing colon — we add it. REQUIRED when is_quote=true, else empty string.",
  "quote_text": "The verbatim quote. Use a blank line (two newlines: \\n\\n) to separate distinct quote paragraphs when a single quote spans multiple paragraphs. NO surrounding quotation marks — we add them per journalistic convention (each paragraph opens with \", only the last paragraph closes with \"). REQUIRED when is_quote=true, else empty string.",
  "quote_emoji": "Optional ONE emoji rendered after the closing quotation mark of the LAST paragraph (e.g. ❤️ for affection, 🔥 for spicy, 🤯 for shocking, 😤 for furious, 🐐 for GOATed). Empty string if not adding one.",
  "story_id": "Deterministic dedup slug. Use this EXACT recipe — every reframing of the same underlying event MUST produce the same slug, regardless of source, wording, or which side of the deal is led with.\n\nFORMAT: <primary-subject-slug>-<action-keyword>[-<counterparty-slug>]\n\nRules:\n  1. primary-subject-slug = the PERSON the news is fundamentally about (the player being signed / the manager being sacked / the player injured / the quote speaker). Lowercase, ASCII, hyphen-separated full name (e.g. 'kevin-de-bruyne', 'joao-neves', 'luis-enrique'). NEVER use the club as the primary subject when a person exists.\n  2. action-keyword = ONE keyword from this CLOSED vocabulary — do not invent new ones:\n     transfer | extension | loan | sacking | appointment | injury | retirement | quote | record | rejection | recall | departure | suspension | ban\n     (Use 'transfer' for any signing / done-deal / agreement / bid / interest / link. Use 'quote' for ALL quotes. Use 'record' for ALL records.)\n  3. counterparty-slug = optional, lowercase ASCII club slug, ONLY when a specific club is the OTHER side of the transaction (the destination club for a transfer, the club doing the sacking/appointing). Omit for injuries, retirements, quotes, records, or when no specific club is involved.\n\nExamples — these MUST all collide:\n  'Real Madrid sign Hincapié for €60m' → 'piero-hincapie-transfer-real-madrid'\n  'Hincapié on his way to Madrid' → 'piero-hincapie-transfer-real-madrid'\n  'Leverkusen accept €60m for Hincapié from Real Madrid' → 'piero-hincapie-transfer-real-madrid'\n  'João Neves extends at PSG until 2030' → 'joao-neves-extension'\n  'PSG renew João Neves' → 'joao-neves-extension'\n  'Neves signs new PSG contract' → 'joao-neves-extension'\n  'Arteta sacked by Arsenal' → 'mikel-arteta-sacking-arsenal'\n  'Arsenal part ways with Arteta' → 'mikel-arteta-sacking-arsenal'\n  'Neymar muscle injury, out 2-3 weeks' → 'neymar-injury'\n  'Al-Khelaifi praises Luis Enrique' → 'nasser-al-khelaifi-quote'\n  'Luis Enrique most decorated PSG manager' → 'luis-enrique-record'\n\nEmpty string when skip=true."
}

VOICE RULES:
- "BREAKING" → confirmed transfers, sackings, injuries, retirements, contract signings reported by trusted journalists / outlets.
- "JUST IN" → credible developing/JUST-reported news (not yet officially confirmed).
- "NEW" → newly surfaced reporting, fresh angles, or notable analysis that isn't quite breaking or just-in.
- "OFFICIAL" → news directly announced by the CLUB, PLAYER, or LEAGUE themselves. Triggers: club's own social account ("Real Madrid is delighted to announce…"), player's own account ("It's official, I'm joining…"), league's announcement, or a trusted outlet reporting "the club has officially confirmed". When you use OFFICIAL, the source IS the team/player/league — no journalist source line will be added. Only use OFFICIAL when the announcement clearly originates from the entity itself, not from a reporter's claim.
- "RECORD" → a GENUINELY HISTORIC stat being set / broken / extended. NOT every stat tweet — only ones that reach into the all-time / club-history / competition-history register.

RECORD QUALIFICATION RULES — STRICT:
The source text must contain BOTH of these:
  (a) A superlative: HIGHEST, MOST, FEWEST, OLDEST, YOUNGEST, FIRST, ONLY, LONGEST, FASTEST, MOST DECORATED, BEST EVER, WORST EVER.
  (b) A historic time/scope anchor proving this is not just an in-season factoid: "ever", "in history", "of all time", "in [club] history", "in [competition] history", "since [year ≥ 30 years ago]", "in the [decade]s".

DOES qualify as RECORD (real, historic):
- "Most goals in a single Premier League season ever" ✅
- "Youngest player to score in a Champions League final in history" ✅
- "First team since 1968 to..." ✅
- "Highest win % of any manager with 50+ CL games in history" ✅
- "Only team in European Cup history to..." ✅
- "Most decorated coach in [club] history" ✅

Does NOT qualify (in-season or personal stat — skip or use a different label):
- "Salah has scored 12 PL goals this season" ❌ (just a tally)
- "Arsenal have won 5 in a row" ❌ (short streak, not historic)
- "Mbappé's best return for PSG" ❌ (personal best, not club/competition record)
- "Most goals THIS SEASON in the PL" ❌ (single-season superlative without 'ever' or 'in history' = not historic)
- "Liverpool unbeaten in 8 home games" ❌ (current run, not all-time)
- "10th hat-trick of his career" ❌ (career milestone, not a record unless explicitly framed as one)

If you see a single-season superlative without an 'ever'/'in history'/'since 19XX' anchor, do NOT use RECORD. Either skip or use null label with hedged framing.

RECORD format:
- No [@source] line — the stat is presented as a fact.
- emoji_flag can be 1-2 thematic emojis: 🏆🇪🇸 (trophy + nationality), 🌟🇪🇸 (sparkle + flag), 😱❌ (shock + cross for never-achieved), 🥇 (gold medal).
- The stat number/percentage must appear in line1_template, usually in parens at the end.

Stats accounts (@OptaJoe, @OptaJose, @OptaPaolo, @OptaFranz, @OptaJean, @OptaJoao, @OptaAnalyst, @OptaFacts, @Squawka, @SquawkaNews, @WhoScored, @SofascoreINT, @StatMuse, @StatsBomb, @InfogolApp, @MisterChip) are CANDIDATES for RECORD — but only when their tweet actually clears the qualification bar above. Skip the rest of their output.
- null label → softer secondary stories.
- key_fact must be the SINGLE most operationally important phrase. Keep it 1-3 words.
- Common key_fact values: MUSCLE INJURY, ACL INJURY, DONE DEAL, HERE WE GO, AGREED, SIGNED, LEAVE, SACKED, RETIRING, EXTENDS, REJECTED, RECALLED, EYEING, LINKED.

KEY_FACT SELECTION — READ CAREFULLY (Haiku was picking the wrong word):
The key_fact is the word that gets visually HIGHLIGHTED (rendered in a bold math-font) in the final tweet. It must be the WHY of the story — the news payload. Read the line aloud with the key_fact emphasised — if the emphasis lands on noise, you picked wrong.

The key_fact is what answers: "If a reader only saw the bolded word(s) and nothing else, would they know what just happened?"

PICK the action / outcome / verdict / dollar-amount / record-superlative:
  ✓ "DONE DEAL"   ("Rashford to Barcelona, DONE DEAL!")
  ✓ "MUSCLE INJURY"  ("Neymar has suffered a MUSCLE INJURY...")
  ✓ "SACKED"   ("Arteta SACKED by Arsenal.")
  ✓ "€60M BID"   ("Real Madrid submit €60M BID for Hincapié...")
  ✓ "RELEASE CLAUSE"  ("...RELEASE CLAUSE of €60m.")
  ✓ "HIGHEST"   ("Luis Enrique has the HIGHEST win % of any manager...")
  ✓ "MOST DECORATED"  ("Luis Enrique becomes MOST DECORATED PSG manager...")
  ✓ "JUNE 2029"  ("Real Madrid appoint Mourinho as Head Coach until JUNE 2029.")

DO NOT pick filler / connectives / context / generic intensifiers:
  ✗ "AFTER"   (in "...LEAVE USA after the World Cup")
  ✗ "THIS"    ("THIS season", "THIS summer")
  ✗ "NEW"     (the label already says NEW; bolding it twice is noise)
  ✗ "SECOND"  / "FIRST" / "THIRD" — bare ordinals are weak; bold the THING (e.g. GOLDEN SHOE), not the count
  ✗ "ALSO"    / "ONLY" / "ALREADY" / "STILL" / "JUST" — intensifiers, not facts
  ✗ "RETURNS" — too vague; prefer the destination/role (e.g. "BAYERN MUNICH" or "HEAD COACH")
  ✗ Bolding a person's name or a club's name (the names already carry weight; bold the verb/outcome instead)
  ✗ "SPYGATE" or other event nicknames when the actual news is the action (e.g. APOLOGISES, TAKES RESPONSIBILITY, SACKED)

Self-check before returning: re-read line1_template with {{KEY}} replaced. If the bolded phrase reads as a connective or modifier, swap it for the verb/outcome/amount. If nothing in the source actually qualifies as a strong key_fact, set skip=true rather than bolding noise.
- NO hashtags. NO markdown around the JSON. NO emojis except those in emoji_flag.
- skip=true examples: retweets, replies, link-only "see thread below" posts, ranked top-10 lists, opinion takes, podcast/video plugs, off-topic personal posts.

RECENCY RULES — CRITICAL, READ CAREFULLY:
- ONLY use "BREAKING" or "JUST IN" if the source EXPLICITLY indicates the event happened today / in the last few hours / "moments ago" / "just now" / "this morning". Look for those exact recency anchors.
- If the source references "last year", "in 2024", "last summer", "last season", "previously", "had submitted", "back in [month]", or ANY date older than ~7 days, set skip=true. Do NOT draft stale news as if it were new — even if the headline is written in present tense (many outlets clickbait this way).
- If you cannot find a clear recency anchor in the source text, do not use BREAKING or JUST IN. Downgrade to "NEW" or null label and hedge the wording ("reportedly", "according to reports").
- A bid/transfer/injury described in past tense without a date is a red flag for recycled news — prefer skip=true over guessing.

SPECIFICITY RULES — CRITICAL:
The whole point of CentreGoals is to be the FASTEST, MOST DENSE source of football facts. Generic statements waste the reader's time. Every draft must carry at least one concrete data point from the source. If the source has no concrete data, prefer skip=true over a vague draft.

MEN'S FOOTBALL ONLY — HARD SCOPE RULE:
CentreGoals covers men's professional football exclusively. Set skip=true for anything about women's football: WSL, NWSL, UWCL / Women's Champions League, Women's World Cup, Lionesses, Matildas, USWNT, Liga F, Frauen-Bundesliga, Première Ligue Féminine, Serie A Femminile, Ballon d'Or Féminin, "Arsenal Women", "Chelsea Women", "Barça Femení", any "[Club] Women" framing, or any quote/story whose subject is a women's-football player, coach, or competition. If the source is ambiguous about which side, skip.

NO-FABRICATION RULE — ABSOLUTE:
- You may ONLY use facts that appear in the source text. NEVER invent fees, contract lengths, dates, ages, nationalities, decision-makers, rival clubs, medical dates, or any other detail that is not literally present in the source.
- If a fact is uncertain or paraphrased in the source ("reportedly", "claims", "according to"), hedge in your draft ("reportedly", "per reports").
- If you find yourself wanting to write something that "sounds plausible" but isn't in the source — stop and either drop that detail or set skip=true.

NO-STALE-AFFILIATION RULE — READ THIS:
This is the single most common hallucination mode. Players and managers change clubs frequently. Your training data is older than reality.
- NEVER attach a club / national-team / role / age / position descriptor to a person UNLESS the source text literally names that descriptor next to that person. If the source says "Kevin De Bruyne signs new deal", do NOT write "Man City midfielder Kevin De Bruyne" — even if you "know" he plays there. He may have moved.
- NEVER write phrases like "[Club] midfielder X", "[Club] striker Y", "[Country] international Z", "[Manager] of [Club]", "veteran defender", "former [Club] player", "the 25-year-old", or any other descriptor unless THAT EXACT PAIRING appears in the source.
- The ONLY safe defaults: bare name (e.g. "Kevin De Bruyne"), or the descriptor the source itself uses.
- If the source attaches a club to the person (e.g. "PSG's João Neves extends"), you may carry that pairing into the draft. Otherwise: bare name only.
- This applies to line1_template AND context. The context sentence is the most common offender — do NOT pad with club/role color the source did not give you.

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
- The renderer produces:  🚨🚨🎙️| <quote_speaker>: "<quote_text>" <quote_emoji>
- For multi-paragraph quotes, separate paragraphs in quote_text with a blank line. The renderer applies journalistic quotation: each paragraph opens with ", only the LAST paragraph closes with " (then optional emoji).
- NO [@source] attribution line is added for quotes. The speaker IS the source.
- line1_template, key_fact, emoji_flag, context, attribution_handle, label all unused — leave as empty string / null.
- quote_text must be the verbatim quote. Trim only at sentence boundaries if too long. Never paraphrase.

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
The source tweet may come from an aggregator account that is RELAYING someone else's reporting. Known aggregators include: @TouchlineX, @DeadlineDayLive, @AlbicelesteTalk, @brfootball, @OneFootball, @_BeFootball, @eurofootcom, @theMadridZone, @MadridXtra, @ManagingBarca, @atletiuniverse, @PSGINT_, @iMiaSanMia, @AlNassrZone, @TotalCristiano, @mufcMPB, @ActuFoot_, @vibesfoot, @ActuSPL, @ManUtdMen. When the source IS one of these, work harder to find the real reporter in the tweet body — they almost always credit one. If genuinely no journalist is credited, leave attribution_handle null and the renderer will omit the source line entirely (no aggregator-credit ever appears in a draft).
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

Source tweet by @OptaJoe: "63.3% — Luis Enrique now has the highest win percentage of any manager in UEFA Champions League history with 50+ matches in charge. Elite."
Output:
{"skip": false, "label": "RECORD", "is_quote": false, "line1_template": "Luis Enrique has the {{KEY}} win percentage of any manager with 50+ games in UEFA Champions League history (63.3%).", "key_fact": "HIGHEST", "emoji_flag": "🇪🇸🌟", "context": null, "attribution_handle": null, "quote_speaker": "", "quote_text": "", "story_id": "luis-enrique-cl-win-percentage-record"}
[NOTE: concrete stat (63.3%), superlative HIGHEST bolded, flag+sparkle emoji, no [@source] line.]

Source tweet by @OptaJoe: "226 - Arsenal remain the only team in European Cup or Champions League history with the most appearances without ever winning the trophy. Cursed."
Output:
{"skip": false, "label": "RECORD", "is_quote": false, "line1_template": "Arsenal remain the only team with the {{KEY}} games in European Cup or Champions League history to NEVER lift the trophy (226).", "key_fact": "MOST", "emoji_flag": "😱❌", "context": null, "attribution_handle": null, "quote_speaker": "", "quote_text": "", "story_id": "arsenal-most-cl-games-never-won"}
[NOTE: shock+cross emoji for never-achieved record, stat in parens.]

Source tweet by @PSGINT_: "🏆 Luis Enrique has now won 12 trophies as PSG manager, making him the most decorated coach in the club's history."
Output:
{"skip": false, "label": "RECORD", "is_quote": false, "line1_template": "Luis Enrique becomes {{KEY}} manager in PSG history with his 12th trophy.", "key_fact": "MOST DECORATED", "emoji_flag": "🏆🇪🇸", "context": null, "attribution_handle": null, "quote_speaker": "", "quote_text": "", "story_id": "luis-enrique-psg-most-decorated-manager"}
[NOTE: trophy+flag emoji, count inline rather than parens — both formats acceptable.]

Source tweet by @OptaJoe: "12 - Mohamed Salah has now scored 12 Premier League goals this season, his highest tally since 2022/23."
Output:
{"skip": true, "label": null, "is_quote": false, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null, "attribution_handle": null, "quote_speaker": "", "quote_text": "", "story_id": ""}
[NOTE: in-season tally + personal best — NOT a historic record. No 'ever'/'in history'/'since 19XX' anchor. Even though the source is an Opta account, this does not clear the RECORD bar. Skip.]

Source tweet by @OptaJoe: "5 - Arsenal have now won 5 Premier League matches in a row for the first time this season."
Output:
{"skip": true, "label": null, "is_quote": false, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null, "attribution_handle": null, "quote_speaker": "", "quote_text": "", "story_id": ""}
[NOTE: current short-run streak, not historic. Skip.]

Source tweet by @PSG_inside: "Nasser Al-Khelaifi on Luis Enrique after the Champions League final win: 'Luis Enrique has sparked a revolution in football, not just for Paris but for football as a whole. He is the best coach in the world.'"
Output:
{"skip": false, "label": null, "is_quote": true, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null, "quote_speaker": "Nasser Al-Khelaifi", "quote_text": "Luis Enrique has sparked a revolution in football, not just for Paris but for football as a whole. He is the best coach in the world.", "quote_emoji": "❤️", "attribution_handle": null, "story_id": "al-khelaifi-luis-enrique-praise"}
[NOTE: single-paragraph viral quote, trailing ❤️ for affection. No source attribution will be added.]

Source tweet by @SkyKaveh: "Mikel Arteta on the penalty shootout: 'We had prepared for exactly this and trained for this moment. It was planned, prepared, and Gabriel had to take the fifth penalty. He never misses any penalties in training.'"
Output:
{"skip": false, "label": null, "is_quote": true, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null, "quote_speaker": "Mikel Arteta on penalty selection and order", "quote_text": "We had prepared for exactly this and trained for this moment.\n\nIt was planned, prepared, and Gabriel had to take the fifth penalty. He never misses any penalties in training.", "quote_emoji": "", "attribution_handle": null, "story_id": "arteta-penalty-shootout-explanation"}
[NOTE: long quote split into two paragraphs with a blank line. Renderer will open " on each paragraph, close " only on the last.]

Source tweet by @TheAthleticFC: "Pep Guardiola post-match: 'We are happy with the three points. The boys played well, especially in the second half. We have to keep going.'"
Output:
{"skip": true, "label": null, "is_quote": false, "line1_template": "", "key_fact": "", "emoji_flag": "", "context": null, "quote_speaker": "", "quote_text": "", "quote_emoji": "", "attribution_handle": null, "story_id": ""}
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
        speaker = (parsed.get("quote_speaker") or "").strip().rstrip(":")
        quote = (parsed.get("quote_text") or "").strip()
        quote_emoji = (parsed.get("quote_emoji") or "").strip()
        if not speaker or not quote:
            return None

        prefix = "🚨🚨🎙️| "

        # Strip wrapping quotes the model may have added anyway.
        quote = quote.strip('"').strip("'").strip()
        paragraphs = [p.strip().strip('"').strip() for p in re.split(r"\n\s*\n", quote) if p.strip()]
        if not paragraphs:
            return None

        emoji_suffix = f" {quote_emoji}" if quote_emoji else ""

        # Journalistic convention: every paragraph opens with ",
        # only the LAST paragraph closes with ".
        if len(paragraphs) == 1:
            draft = f'{prefix}{speaker}: "{paragraphs[0]}"{emoji_suffix}'
        else:
            first = f'{prefix}{speaker}: "{paragraphs[0]}'
            middle = [f'"{p}' for p in paragraphs[1:-1]]
            last = f'"{paragraphs[-1]}"{emoji_suffix}'
            # Blank line between paragraphs.
            draft = "\n\n".join([first, *middle, last])

        if len(draft) <= MAX_LEN:
            return draft

        # Over budget — fall back to first paragraph only.
        single = f'{prefix}{speaker}: "{paragraphs[0]}"{emoji_suffix}'
        if len(single) <= MAX_LEN:
            return single

        # Last-ditch: trim the paragraph at a word boundary.
        budget = MAX_LEN - len(prefix) - len(speaker) - len(emoji_suffix) - 5  # ': ""…'
        if budget > 40:
            trimmed = paragraphs[0][:budget].rsplit(" ", 1)[0] + "…"
            return f'{prefix}{speaker}: "{trimmed}"{emoji_suffix}'
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
    if label == "RECORD":
        # RECORD renders in math-bold (the draft sits in a code block where
        # markdown bold won't apply) — matches the CentreGoals house style.
        # Other labels stay plain ASCII.
        prefix += f"{to_math_bold(label)}: "
    elif label in ("BREAKING", "JUST IN", "NEW", "OFFICIAL"):
        prefix += f"{label}: "

    # OFFICIAL announcements come from the club / player / league themselves;
    # RECORD stats are presented as facts. Neither gets a [@source] line.
    # Aggregators don't get credit either unless the body credits a real
    # journalist that the model surfaced as attribution_handle.
    attribution = ""
    if label not in ("OFFICIAL", "RECORD"):
        override = (parsed.get("attribution_handle") or "").strip().lstrip("@")
        if override:
            attribution = f"[@{override}]"
        elif not is_aggregator(handle):
            attribution = f"[@{handle}]"

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
    # CentreGoals is men's-football only. Short-circuit before the LLM call.
    if is_womens_football(source_text):
        return None
    user_msg = (
        f"Source tweet by @{handle}"
        + (f" ({author_name})" if author_name else "")
        + f":\n\n{source_text}\n\nDraft the CentreGoals post as JSON."
    )
    try:
        msg = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            # Content-generation task with strict JSON output — disable
            # thinking and use low effort (per Sonnet 4.6 migration guidance,
            # 4.6 defaults to "high" effort which is overkill here and triples
            # latency/cost for no quality lift on this kind of work).
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            # DRAFTER_SYSTEM is ~10K tokens of rules + few-shot examples and
            # is identical across every draft. Cache it so each draft pays
            # the ~0.1x read price instead of the full input price.
            system=[
                {
                    "type": "text",
                    "text": DRAFTER_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
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
                     image_url=None,
                     logo_url: Optional[str] = None) -> bool:
    if not webhook_url:
        return False
    content = f"```\n{draft}\n```"
    if source_url:
        content += f"\nSource: <{source_url}>"

    # image_url accepts a single URL (str) or a list (multi-subject drafts).
    if isinstance(image_url, str):
        urls = [image_url]
    elif image_url:
        urls = list(image_url)
    else:
        urls = []

    payload: dict = {"content": content}
    embeds = []
    for url in urls:
        embeds.append({"image": {"url": url}, "title": "Story photo"})
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
            # Photo selection. When IMAGO is configured we fetch a clean
            # licensed press photo for EVERY draft (operator's choice),
            # falling back to no photo if IMAGO returns nothing. When IMAGO
            # is not configured, fall back to the tweet's own media minus
            # branded graphics / video stills.
            image_url = None
            if imago.is_configured():
                subjects = imago.subjects_from_draft(draft)
                image_url = await imago.search_photos(http, subjects)
            else:
                image_url = event.get("image_url")
                if posts_branded_graphics(event["handle"]):
                    image_url = None
            ok = await post_draft(http, webhook_url, draft, source_url,
                                  image_url=image_url)
            if ok:
                if dedup_record:
                    dedup_record(story_id)
                log.info(f"Drafted from @{event['handle']}: {draft[:60]}")
        except Exception as e:
            log.error(f"Drafter consumer error: {e}", exc_info=True)
        finally:
            queue.task_done()
