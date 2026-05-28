# ⚽ Football Ops Alert Bot

A production-ready Discord bot that monitors football news in **any language**, translates alerts to English, and routes them to the right Discord channel by league — automatically.

---

## What It Does

| Feature | Detail |
|---|---|
| 📡 **News monitoring** | Polls 30+ Google News RSS feeds every 75 seconds |
| 🌍 **Multi-language** | Detects articles in Spanish, Italian, German, French, Turkish, Arabic, Dutch, Portuguese… |
| 🤖 **AI translation** | Uses Claude to translate headlines + summaries into clean English |
| 🎯 **Smart routing** | Routes to the correct Discord channel by league & club |
| 🧩 **Deduplication** | Clusters related stories within 6-hour windows, suppresses duplicates |
| 🚨 **Urgency scoring** | 1–5 scale; breaking news pings `@here` automatically |
| 💾 **Persistent DB** | SQLite stores all articles, clusters, and post history |
| 📝 **CentreGoals tweet drafter** | Listens to top journalists on X via filtered stream + outlet RSS; generates pre-written tweets in CentreGoals voice and drops them into a dedicated Discord channel for one-click copy/paste. Goal: be FIRST. |

---

## Clubs & Channels

| Channel | Clubs |
|---|---|
| 🏴󠁧󠁢󠁥󠁮󠁧󠁿 `#premier-league` | Man Utd, Man City, Liverpool, Chelsea, Arsenal, Tottenham, Newcastle, Aston Villa |
| 🇪🇸 `#la-liga` | Real Madrid, Barcelona, Atlético Madrid |
| 🇮🇹 `#serie-a` | Como 1907, Juventus, Inter Milan, AC Milan, Napoli |
| 🇩🇪 `#bundesliga` | Bayern Munich, Dortmund, Bayer Leverkusen |
| 🇫🇷 `#ligue-1` | PSG, Marseille |
| 🌍 `#other-leagues` | Ajax, Porto, Sporting, Galatasaray, Beşiktaş, Fenerbahçe, Inter Miami, Al-Nassr, Al Hilal |

---

## Urgency Scale

| Level | Label | Meaning | Notification |
|---|---|---|---|
| 5 | 🔴 BREAKING | Transfer confirmed, sacking | `@here` ping |
| 4 | 🚨 High | Strong rumour, major injury | Red embed |
| 3 | 🔔 Notable | Developing story | Orange embed |
| 2 | 📋 Medium | Background news | Blue embed |
| 1 | 📰 Low | Filler / minor | Grey embed |

---

## Quick Start (Local)

### Prerequisites
- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)
- Discord webhooks for each channel (see below)

### 1. Clone & Install

```bash
git clone <your-repo>
cd football-ops-bot
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env and fill in your API keys and webhook URLs
```

### 3. Test Your Webhooks First

```bash
# Load your .env then run:
export $(cat .env | xargs)
python test_webhooks.py
```

You should see a test embed appear in each configured Discord channel.

### 4. Run the Bot

```bash
python bot.py
```

---

## Docker (Local)

```bash
cp .env.example .env
# fill in .env

docker compose up --build
```

The SQLite database persists in a Docker volume called `football_data`.

---

## Deploy to Render (Non-Coder Guide)

Render is a cloud platform. The free tier is enough to run this bot.

### Step 1: Push to GitHub

1. Create a new repository on [github.com](https://github.com)
2. Upload all the bot files (bot.py, requirements.txt, Dockerfile, etc.)

### Step 2: Create a Render Account

Go to [render.com](https://render.com) and sign up (free).

### Step 3: Create a New Web Service

1. Click **New +** → **Web Service**
2. Connect your GitHub repo
3. Configure:
   - **Name:** `football-ops-bot`
   - **Environment:** `Docker`
   - **Instance Type:** `Starter` (free tier works)
   - **Region:** Choose closest to you

### Step 4: Add a Persistent Disk

1. In your service settings, go to **Disks**
2. Click **Add Disk**
3. Set:
   - **Name:** `football-data`
   - **Mount Path:** `/data`
   - **Size:** 1 GB (free tier allows 1 GB)

### Step 5: Add Environment Variables

In **Environment** tab, add each variable:

| Key | Value |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `PREMIER_LEAGUE_WEBHOOK` | `https://discord.com/api/webhooks/...` |
| `LA_LIGA_WEBHOOK` | `https://discord.com/api/webhooks/...` |
| `SERIE_A_WEBHOOK` | `https://discord.com/api/webhooks/...` |
| `BUNDESLIGA_WEBHOOK` | `https://discord.com/api/webhooks/...` |
| `LIGUE_1_WEBHOOK` | `https://discord.com/api/webhooks/...` |
| `OTHER_LEAGUES_WEBHOOK` | `https://discord.com/api/webhooks/...` |
| `TWEET_DRAFTS_WEBHOOK` | `https://discord.com/api/webhooks/...` (CentreGoals draft channel) |
| `TWITTER_BEARER_TOKEN` | X API v2 Bearer Token (Basic tier+ for streaming) |
| `DB_PATH` | `/data/football_ops.db` |

### Step 6: Deploy

Click **Create Web Service**. Render will build the Docker image and start the bot. You'll see live logs.

---

## How to Get Discord Webhook URLs

For each channel you want alerts in:

1. Open Discord → Go to the channel
2. Click the ⚙️ gear (Channel Settings)
3. Click **Integrations** → **Webhooks**
4. Click **New Webhook**
5. Give it a name (e.g. "Football Ops Bot")
6. Click **Copy Webhook URL**
7. Paste that URL as the environment variable

---

## File Structure

```
football-ops-bot/
├── bot.py              # Main bot — pipeline, polling, AI, Discord
├── test_webhooks.py    # Test script for all webhooks
├── requirements.txt    # Python dependencies
├── Dockerfile          # Docker build instructions
├── docker-compose.yml  # Local Docker Compose setup
├── .env.example        # Environment variable template
└── README.md           # This file
```

---

## Database

SQLite database at `DB_PATH` (default: `football_ops.db`).

```sql
-- View recent posted alerts
SELECT title_en, league, club, urgency, datetime(fetched_at, 'unixepoch') as time
FROM articles
WHERE posted = 1
ORDER BY fetched_at DESC
LIMIT 20;

-- View cluster activity
SELECT id, topic, article_count, datetime(first_seen, 'unixepoch')
FROM clusters
ORDER BY last_updated DESC
LIMIT 10;
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| No alerts appearing | Check `ANTHROPIC_API_KEY` is set correctly |
| Wrong channel | Check club name matches `LEAGUE_CLUBS` in `bot.py` |
| Duplicate alerts | Normal deduplication takes ~6 hours per story cluster |
| Webhook 404 | Regenerate the webhook URL in Discord |
| Bot stopped | Check Render logs; restart service |

---

## Customisation

**Add a new club:** Edit `LEAGUE_CLUBS` dict in `bot.py` under the right league key.

**Add a search query:** Add to `SEARCH_QUERIES` list in `bot.py`.

**Change poll interval:** Edit `POLL_INTERVAL = 75` (seconds).

**Change cluster window:** Edit `CLUSTER_WINDOW = 6 * 3600` (seconds).
