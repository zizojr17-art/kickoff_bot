# ⚽ Kickoff Bot

A production Discord bot for World Cup & football match notifications. Monitors live matches, sends real-time alerts, runs score prediction polls, hosts MOTM voting, and gives every user personal notification controls — all without spamming non-interested members.

---

## Requirements

- Python 3.11+
- `discord.py` 2.3+
- `football-data.org` API key (free tier supported)
- The following files must exist alongside `bot.py`:
  - `state.py` — persistent bot state (JSON-backed)
  - `embeds.py` — all Discord embed builders + colour constants
  - `football_api.py` — football-data.org API wrapper

---

## Setup

### 1 — Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_BOT_TOKEN` | ✅ | Your bot token from the Discord developer portal |
| `FOOTBALL_DATA_API_KEY` | ✅ | API key from football-data.org |

### 2 — Discord developer portal

1. Enable **Message Content Intent** (required for `!` prefix commands)
2. Enable **Server Members Intent** (required for leaderboard member lookups)
3. Invite the bot with these OAuth2 scopes: `bot`, `applications.commands`
4. Required bot permissions: `Send Messages`, `Embed Links`, `Add Reactions`, `Manage Messages`, `Read Message History`, `Pin Messages`

### 3 — Run

```bash
python bot.py
```

---

## First-time server setup

Run these commands **in order** in your server as an admin. Each one is a slash (`/`) command.

| Step | Command | What it does |
|------|---------|--------------|
| 1 | `/setchannel` | Sets the channel for live match alerts (goals, cards, kickoff, FT) |
| 2 | `/setpredictionschannel` | Sets the channel for prediction polls & MOTM voting |
| 3 | `/setresultschannel` | Sets the channel for prediction results & MOTM winners |
| 4 | `/setsummarychannel` | Sets the channel for match recaps & YouTube highlights |
| 5 | `/setcommandschannel` | Sets the channel for the mode picker (Quiet / Standard / Detailed) |
| 6 | `/setmode` | Sets the guild notification verbosity (default: `standard`) |
| 7 | `/settimezone` | Sets your server timezone for daily summaries (default: `UTC`) |
| 8 | `/setinteractivechannel` | Posts the interactive panel — users self-manage their preferences here |

You can skip any channel you don't want. Only `setchannel` is needed for basic live alerts.

---

## Notification modes (guild-wide)

Set with `/setmode` or via the mode picker posted by `/setcommandschannel`.

| Mode | What you get |
|------|-------------|
| 🔇 **Quiet** | Goals + Red Cards + Half Time + Full Time |
| 📢 **Standard** | Quiet + Extra Time, Penalty Shootout, MOTM voting, Prediction polls |
| 📋 **Detailed** | Everything: lineups, second-half starts, recaps, highlights, group tables |

Users can individually **override** these with their own per-notification settings (see below).

---

## Per-user notification settings

Every user can control exactly which notifications they receive. Settings are **ephemeral** — only the user themselves can see them.

### Via the interactive panel
Post the panel in any channel with `/setinteractivechannel`. Users click:
- **🔔 My Notifications** — toggle individual notification types ON/OFF
- **⭐ My Teams** — view followed teams (with unfollow option)
- **🏆 My Competitions** — view followed competitions (with unfollow option)
- **🏅 Leaderboard** — view current prediction standings
- **📅 Today's Matches** — see today's schedule

### Via slash commands
| Command | Description |
|---------|-------------|
| `/mysettings` | Open the settings panel (toggle notifications) |
| `/viewsettings` | Read-only view of your current settings |
| `/resetmysettings` | Reset all personal settings back to server defaults |

### Toggleable notification types
Goals · Red Cards · Kickoff · Half Time · Full Time · Lineups · MOTM Voting · MOTM Results · Prediction Polls · Prediction Results · Recaps · Daily Summary

---

## Following teams & competitions

Users can follow teams and competitions to track them personally.

| Command | Description |
|---------|-------------|
| `/followteam <name>` | Follow a team e.g. `Brazil`, `Arsenal` |
| `/unfollowteam <name>` | Unfollow a team |
| `/myteams` | See your followed teams (with unfollow buttons) |
| `/followcompetition <code>` | Follow a competition e.g. `WC`, `PL`, `CL` |
| `/unfollowcompetition <code>` | Unfollow a competition |
| `/mycompetitions` | See your followed competitions (with unfollow buttons) |

Limits: 20 teams, 15 competitions per user.

---

## Score prediction system

- **90 min before kickoff** — prediction poll is posted to the predictions channel
- Users click **🎯 Predict Score** → get a private score picker (− / + buttons for each team)
- Predictions can be updated until kickoff
- At kickoff, the poll is locked automatically
- **Knockout matches** — draws are blocked (must predict a winner)
- After full time, scores are automatically: **exact score = 3 pts**, **correct result = 1 pt**

---

## MOTM voting

- Sent ~75 minutes into a match to the predictions channel
- Nominee list is built from: goal scorers (+2 pts), assisters (+1 pt), starting lineup with position weights, captain bonus
- Targeting 8–10 nominees — padded from bench if needed
- Users vote via dropdown (can change until full time)
- Results posted to the results channel after full time

---

## Leaderboard

- Auto-updated in the results channel after every match
- Newest leaderboard pinned, old one unpinned automatically
- View at any time with `/leaderboard` or via the interactive panel
- Reset with `/resetleaderboard` (admin)

---

## All slash commands

### Info commands
| Command | Description |
|---------|-------------|
| `/today` | Today's matches |
| `/matchtoday [competition]` | Today's matches, filtered by competition |
| `/live` | Currently live matches |
| `/nextmatch [competition]` | Next upcoming match |
| `/standings [competition]` | Competition standings |
| `/upcoming [competition] [days]` | Upcoming matches (1–30 days) |
| `/team <name>` | Team profile + recent/upcoming fixtures |
| `/match <id>` | Match detail by football-data.org ID |
| `/group <letter>` | World Cup group table (A–P) |
| `/worldcup` | Full World Cup overview — all groups |
| `/bracket` | World Cup knockout bracket |
| `/competitions` | List of supported competition codes |

### Personal settings
| Command | Description |
|---------|-------------|
| `/mysettings` | Toggle your personal notifications |
| `/viewsettings` | View your current settings |
| `/resetmysettings` | Reset to server defaults |
| `/followteam <name>` | Follow a team |
| `/unfollowteam <name>` | Unfollow a team |
| `/myteams` | See your followed teams |
| `/followcompetition <code>` | Follow a competition |
| `/unfollowcompetition <code>` | Unfollow a competition |
| `/mycompetitions` | See your followed competitions |
| `/leaderboard` | View the prediction leaderboard |
| `/timezone` | Show server timezone |
| `/help` | Full command list |

### Admin commands (require Manage Channels)
| Command | Description |
|---------|-------------|
| `/setchannel` | Set the live match notifications channel |
| `/setpredictionschannel` | Set the predictions & MOTM channel |
| `/setresultschannel` | Set the results channel |
| `/setsummarychannel` | Set the recaps & highlights channel |
| `/setcommandschannel` | Set the mode picker channel |
| `/setinteractivechannel` | Post the interactive self-service panel |
| `/setmode` | Set guild notification mode |
| `/settimezone` | Set guild timezone |
| `/status` | Show bot config for this server |
| `/updatecommandsmenu` | Rebuild and repost the mode picker |

### Admin commands (require Administrator)
| Command | Description |
|---------|-------------|
| `/resetleaderboard` | Reset the prediction leaderboard |
| `/testembed <type>` | Preview any notification embed with test data |

---

## Prefix commands (`!`)

All slash commands have a `!` prefix equivalent. Requires **Message Content Intent** enabled in the developer portal.

Examples: `!today`, `!live`, `!standings WC`, `!team Brazil`, `!upcoming CL 14`

---

## Testing embeds

Use `/testembed <type>` (admin only) to preview any notification without waiting for a real match.

Available types: `reminder15` · `kickoff` · `lineup` · `goal` · `redcard` · `halftime` · `secondhalf` · `extratime` · `pso` · `fulltime` · `motm` · `recap` · `prediction`

The `prediction` type includes fully working score-selection buttons.

---

## embeds.py — 15-min reminder fix

If the 15-minute reminder shows "1 hour" instead of "15 minutes", the fix is in your local `embeds.py`. Find the `embed_reminder(match, minutes)` function and ensure it uses the `minutes` parameter:

```python
# CORRECT
description = f"⏰ Kick-off in {minutes} minute{'s' if minutes != 1 else ''}"

# WRONG — hardcoded
description = "⏰ Kick-off in 1 hour"
```

`bot.py` always passes `15` correctly — the bug is only in `embeds.py`.

---

## Background loops

| Loop | Interval | What it does |
|------|----------|--------------|
| `monitor_loop` | Every 2 min | Detects match events: kick-off, goals, cards, HT, FT, lineup release, reminders, MOTM trigger |
| `daily_summary_loop` | Every 5 min | Sends daily summary at 23:50 in the guild's configured timezone |

Both loops are safe on Discord reconnect — they check `is_running()` before starting.

---

## State storage

All data is persisted by `state.py` to a local JSON file. Survives restarts. Structure:

- Guild configs (channels, mode, timezone)
- Per-match sent events, live snapshots, goal/card announcements
- Score predictions per match per user
- MOTM votes per match per guild
- Leaderboard per guild
- Per-user notification preferences (per guild)
- Per-user followed teams and competitions
- Interactive panel message IDs

---

## Competition codes

| Code | Competition |
|------|-------------|
| `WC` | FIFA World Cup |
| `PL` | Premier League |
| `CL` | UEFA Champions League |
| `EURO` | UEFA European Championship |
| `BL1` | Bundesliga |
| `SA` | Serie A |
| `PD` | La Liga |
| `FL1` | Ligue 1 |
| `PPL` | Primeira Liga |
| `EC` | European Championship (qualifier) |

Run `/competitions` in Discord for the full live list.

---

## Changelog

### v2.0 (current)
- Added per-user notification settings (`/mysettings`, `/viewsettings`, `/resetmysettings`)
- Added team following (`/followteam`, `/unfollowteam`, `/myteams`)
- Added competition following (`/followcompetition`, `/unfollowcompetition`, `/mycompetitions`)
- Added persistent interactive panel (`/setinteractivechannel`)
- Fixed MOTM nominees — expanded position matching, now targets 8–10 nominees reliably
- Fixed `/testembed prediction` — now includes working score-selection buttons
- Fixed `on_ready` crash on Discord reconnect (loop double-start)
- Fixed empty embed list leaving deferred interaction hanging
- Fixed shallow-copy state mutation in follow/unfollow commands
- Fixed `InteractivePanel.btn_today` crash when no matches are scheduled
- Fixed `_process_fulltime` crash when match detail API call returns None

### v1.0 (original)
- Live match monitoring (goals, cards, HT, FT, kickoff)
- Score prediction polls with leaderboard
- MOTM voting
- World Cup group tables and bracket
- Guild notification modes (quiet / standard / detailed)
