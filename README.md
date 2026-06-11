# Kickoff Bot 🏆

Discord bot for **FIFA World Cup 2026** and general football notifications.

## Quick Start

1. Add the bot to your server with the invite link
2. In Discord, run these five channel-setup commands in the right channels:

| Command | Channel purpose |
|---|---|
| `/setchannel` | Live match events (goals, cards, HT, FT, lineups, daily summary) |
| `/setpredictionschannel` | Score prediction polls + MOTM voting |
| `/setresultschannel` | MOTM winners, prediction results, points awarded |
| `/setleaderboardchannel` | Pinned leaderboard |
| `/setcommandschannel` | Commands menu + mode picker (posts automatically) |

3. `/setmode` — choose the server's notification verbosity (default: standard)
4. `/settimezone` — set local timezone for the daily summary

---

## Guild Modes

| Mode | What you get |
|---|---|
| **quiet** | Goals · Red cards · Half-time · Full time |
| **standard** | ↑ + Extra time · Penalty shootout · Score predictions · MOTM voting |
| **detailed** | ↑ + Lineups · 2nd-half notice · 1-hour recap · YouTube highlights · WC group tables |

Set with `/setmode quiet|standard|detailed`

---

## Features

### 1. Score Predictions
- **85–95 min before kick-off**: prediction poll posts to the predictions channel
- Users tap **🎯 Predict Score** → ephemeral +/- score picker → **🔒 Lock Prediction**
- Predictions can be updated until the **poll closes at kick-off**
- **Scoring**: Exact score = **3 pts** · Correct result = **1 pt** (no stacking)

### 2. Lineups (Detailed mode)
- **55–70 min before kick-off**: if confirmed lineups are available, formation + squad posted

### 3. Live Monitoring
- Loops every 2 min; announces goals, red cards, half-time, FT, ET, PSO
- Discord `<t:…>` timestamps render in every user's local timezone

### 4. MOTM Voting (Standard+)
- At ~75ʹ (elapsed), MOTM vote sent to predictions channel
- Nominees from goals/assists and starting lineups (max 10)
- Tied votes → all voters for tied players win
- **MOTM correct vote = +1 pt**

### 5. Leaderboard
- Auto-posted and pinned to the leaderboard channel after every match
- Previous pin unpinned; newest pinned
- `/leaderboard` — view current standings
- `/resetleaderboard` — wipe (admin)

### 6. 1-Hour Recap (Detailed mode)
- Posted 1 hour after full time
- Includes match stats, top performers, YouTube highlights (scraped — no API key needed)

### 7. WC Group Tables (Detailed mode)
- Posted after the daily summary
- Latest group table pinned; previous unpinned

### 8. Commands Menu
- Persistent embed in commands channel with **🔇 Quiet / 📢 Standard / 📋 Detailed** buttons
- Each user saves their own preferred notification mode
- `/updatecommandsmenu` — rebuild menu

---

## All Commands

### Match Info
| Command | Description |
|---|---|
| `/today` | Today's matches |
| `/matchtoday [comp]` | Today filtered by competition |
| `/live` | Currently live matches |
| `/nextmatch [comp]` | Next upcoming match |
| `/match <id>` | Match by football-data.org ID |
| `/upcoming [comp] [days]` | Upcoming fixtures |

### Tournament
| Command | Description |
|---|---|
| `/standings [comp]` | League or group standings |
| `/group <A–P>` | WC 2026 single group table |
| `/worldcup` | All WC 2026 groups overview |
| `/bracket` | WC 2026 knockout bracket |

### Predictions
| Command | Description |
|---|---|
| `/leaderboard` | Prediction points leaderboard |
| `/resetleaderboard` | Wipe leaderboard (admin) |

### Setup (requires Manage Channels)
| Command | Description |
|---|---|
| `/setchannel` | Live match events channel |
| `/setpredictionschannel` | Polls & MOTM channel |
| `/setresultschannel` | Results & points channel |
| `/setleaderboardchannel` | Leaderboard channel |
| `/setcommandschannel` | Commands menu channel |
| `/setmode` | Guild notification verbosity |
| `/settimezone` | Server timezone |
| `/status` | View current bot config |

### Other
| Command | Description |
|---|---|
| `/competitions` | List competition codes |
| `/team <name>` | Team profile & fixtures |
| `/updatecommandsmenu` | Rebuild commands channel menu |
| `/testembed <type>` | Preview embed (admin) |
| `/help` | All commands |

---

## Setup & Environment

```bash
# Required secrets (Replit Secrets panel)
DISCORD_BOT_TOKEN   # from discord.com/developers → Bot → Token
FOOTBALL_API_KEY    # from football-data.org (free tier)
```

### Discord Developer Portal — required settings
- **Message Content Intent**: OFF (not needed)
- **Bot Permissions**: Send Messages · Embed Links · Add Reactions · Read Message History · Manage Messages (for pin/unpin)

### Token expired / 401 error?
Go to [discord.com/developers](https://discord.com/developers) → Applications → Your bot → Bot → **Reset Token** → copy the new token → update the `DISCORD_BOT_TOKEN` secret in Replit.

---

## File Structure

```
discord-bot/
├── bot.py          # Main bot, slash commands, background loops, Discord Views
├── state.py        # JSON persistence with atomic writes
├── football_api.py # football-data.org v4 API + YouTube highlight scraper
├── embeds.py       # All Discord embed builders
├── state.json      # Auto-generated persistent state
└── README.md       # This file
```
