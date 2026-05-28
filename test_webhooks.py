"""
Test script: posts a sample embed to each configured Discord webhook.
Usage: python test_webhooks.py
"""

import asyncio
import os
import sys
from datetime import datetime

import httpx

WEBHOOKS = {
    "premier_league": ("PREMIER_LEAGUE_WEBHOOK", "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League", 0x3D195B),
    "la_liga": ("LA_LIGA_WEBHOOK", "🇪🇸 La Liga", 0xFF4500),
    "serie_a": ("SERIE_A_WEBHOOK", "🇮🇹 Serie A", 0x0066CC),
    "bundesliga": ("BUNDESLIGA_WEBHOOK", "🇩🇪 Bundesliga", 0xFF0000),
    "ligue_1": ("LIGUE_1_WEBHOOK", "🇫🇷 Ligue 1", 0x003399),
    "other": ("OTHER_LEAGUES_WEBHOOK", "🌍 Other Leagues", 0x2ECC71),
}

SAMPLE_ALERTS = {
    "premier_league": {
        "title": "[TEST] Arsenal close to signing world-class striker",
        "summary": "Arsenal are reportedly in advanced talks to sign a top European striker for a club-record fee. "
                   "The deal is expected to be announced before the transfer window closes. "
                   "The player has agreed personal terms and passed a medical.",
        "urgency": "🚨 High",
        "club": "Arsenal",
        "tags": ["`transfer`", "`signing`", "`striker`"],
    },
    "la_liga": {
        "title": "[TEST] Real Madrid injury update ahead of Clasico",
        "summary": "Real Madrid confirm Vinicius Jr has suffered a muscle strain and is doubtful for the upcoming "
                   "El Clasico. The Brazilian winger will undergo further scans on Monday. "
                   "This is a significant blow for Carlo Ancelotti's side.",
        "urgency": "🔔 Notable",
        "club": "Real Madrid",
        "tags": ["`injury`", "`clasico`", "`vinicius`"],
    },
    "serie_a": {
        "title": "[TEST] Inter Milan manager contract extension confirmed",
        "summary": "Inter Milan have officially confirmed Simone Inzaghi has signed a two-year contract extension. "
                   "The deal keeps him at the San Siro until 2027. "
                   "Club president Beppe Marotta hailed it as a key moment for the project.",
        "urgency": "📋 Medium",
        "club": "Inter Milan",
        "tags": ["`contract`", "`manager`", "`inzaghi`"],
    },
    "bundesliga": {
        "title": "[TEST] Bayern Munich confirm Champions League squad list",
        "summary": "Bayern Munich have submitted their UEFA Champions League squad list for the knockout phase. "
                   "New signing Leon Goretzka is included after recovering from injury. "
                   "Three academy players have also been named in the extended squad.",
        "urgency": "📰 Low",
        "club": "Bayern Munich",
        "tags": ["`champions-league`", "`squad`", "`UEFA`"],
    },
    "ligue_1": {
        "title": "[TEST] PSG agree mega-deal for Brazilian midfielder",
        "summary": "Paris Saint-Germain have reportedly agreed a €90m fee with Flamengo for their star midfielder. "
                   "The player is expected to fly to Paris tonight for his medical. "
                   "This would be PSG's biggest summer signing under their new sporting director.",
        "urgency": "🔴 BREAKING",
        "club": "PSG",
        "tags": ["`transfer`", "`breaking`", "`PSG`"],
    },
    "other": {
        "title": "[TEST] Galatasaray win league title with record points tally",
        "summary": "Galatasaray have clinched the Turkish Süper Lig title with three games to spare. "
                   "The Istanbul giants finished with a record points total of 94. "
                   "Manager Okan Buruk dedicated the title to the club's fans.",
        "urgency": "🔔 Notable",
        "club": "Galatasaray",
        "tags": ["`title`", "`superlig`", "`champions`"],
    },
}

URGENCY_COLORS = {
    "📰 Low": 0x808080,
    "📋 Medium": 0x3498DB,
    "🔔 Notable": 0xF39C12,
    "🚨 High": 0xE74C3C,
    "🔴 BREAKING": 0xFF0000,
}


async def send_test_embed(
    client: httpx.AsyncClient,
    league_key: str,
    env_var: str,
    league_label: str,
    color: int,
) -> bool:
    webhook_url = os.environ.get(env_var, "")
    if not webhook_url:
        print(f"  ⚠️  {league_label}: {env_var} not set — skipping")
        return False

    sample = SAMPLE_ALERTS[league_key]
    urgency_text = sample["urgency"]
    embed_color = URGENCY_COLORS.get(urgency_text, color)

    embed = {
        "title": sample["title"],
        "url": "https://example.com/test",
        "description": sample["summary"],
        "color": embed_color,
        "fields": [
            {"name": "Urgency", "value": urgency_text, "inline": True},
            {"name": "League", "value": league_label, "inline": True},
            {"name": "Club", "value": sample["club"], "inline": True},
            {"name": "Tags", "value": " ".join(sample["tags"]), "inline": False},
        ],
        "footer": {
            "text": f"⚽ Football Ops Bot — Test Alert | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        },
        "timestamp": datetime.utcnow().isoformat(),
    }

    payload = {
        "content": "🧪 **This is a test alert from Football Ops Bot** — your webhook is connected!",
        "embeds": [embed],
    }

    try:
        resp = await client.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            print(f"  ✅ {league_label}: embed posted successfully")
            return True
        else:
            print(f"  ❌ {league_label}: HTTP {resp.status_code} — {resp.text[:100]}")
            return False
    except Exception as e:
        print(f"  ❌ {league_label}: {e}")
        return False


async def send_tweet_drafts_test(client: httpx.AsyncClient) -> bool:
    webhook_url = os.environ.get("TWEET_DRAFTS_WEBHOOK", "")
    if not webhook_url:
        print("  ⚠️  Tweet Drafts: TWEET_DRAFTS_WEBHOOK not set — skipping")
        return False

    sample_tweet = (
        "🚨🚨| BREAKING: This is a 𝐓𝐄𝐒𝐓 message from the CentreGoals tweet drafter. ✅🏴‍☠️\n\n"
        "If you see this, the webhook is wired up correctly.\n\n"
        "[@CentreGoalsBot]"
    )
    embed = {
        "title": "📝 Tweet Draft Ready",
        "description": f"```\n{sample_tweet}\n```",
        "color": 0x1DA1F2,
        "fields": [
            {"name": "Category", "value": "Test", "inline": True},
            {"name": "Length", "value": f"{len(sample_tweet)}/280", "inline": True},
            {"name": "Source", "value": "[Article](https://example.com/test)", "inline": True},
        ],
        "footer": {"text": "From: Test Source"},
        "timestamp": datetime.utcnow().isoformat(),
    }
    payload = {"embeds": [embed]}
    try:
        resp = await client.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            print("  ✅ Tweet Drafts: embed posted successfully")
            return True
        print(f"  ❌ Tweet Drafts: HTTP {resp.status_code} — {resp.text[:100]}")
        return False
    except Exception as e:
        print(f"  ❌ Tweet Drafts: {e}")
        return False


async def main():
    print("\n⚽  Football Ops Alert Bot — Webhook Test")
    print("=" * 50)

    configured = sum(1 for env, _, _ in WEBHOOKS.values() if os.environ.get(env))
    if os.environ.get("TWEET_DRAFTS_WEBHOOK"):
        configured += 1
    print(f"Found {configured} webhooks configured (league + drafts)\n")

    if configured == 0:
        print("❌ No webhooks configured. Set the environment variables and retry.")
        sys.exit(1)

    results = []
    async with httpx.AsyncClient() as client:
        for league_key, (env_var, label, color) in WEBHOOKS.items():
            result = await send_test_embed(client, league_key, env_var, label, color)
            results.append(result)
            await asyncio.sleep(0.5)  # avoid rate limits

        # Also test the CentreGoals tweet drafts webhook
        drafts_result = await send_tweet_drafts_test(client)
        if os.environ.get("TWEET_DRAFTS_WEBHOOK"):
            results.append(drafts_result)

    passed = sum(results)
    total = len([v for _, v in zip(results, WEBHOOKS.values()) if os.environ.get(v[0])])
    print(f"\n{'='*50}")
    print(f"Results: {passed}/{configured} webhooks received test embeds")

    if passed == configured:
        print("🎉 All configured webhooks are working!")
    else:
        print("⚠️  Some webhooks failed — check URLs and permissions above.")

    sys.exit(0 if passed == configured else 1)


if __name__ == "__main__":
    asyncio.run(main())
