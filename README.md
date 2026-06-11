# ⚽ FIFA World Cup 2026 "Kickoff" — Discord Bot

A full-featured Discord companion bot for the FIFA World Cup 2026. Live scores, predictions, standings, player stats, match facts, tournament dashboards, MOTM voting, and more — all from a single bot hardcoded to WC 2026.

---

## Requirements

| Requirement | Details |
|---|---|
| Python | 3.11+ |
| discord.py | 2.3+ |
| aiohttp | 3.9+ |
| `DISCORD_BOT_TOKEN` | From the [Discord Developer Portal](https://discord.com/developers/applications) |
| `FOOTBALL_DATA_API_KEY` | Free key from [football-data.org](https://www.football-data.org/client/register) |

Install dependencies:
```bash
pip install -r requirements.txt
```

---

## Files

```
bot.py              — Main bot (all slash commands, loops, views)
football_api.py     — football-data.org API wrapper (async)
state.py            — Guild + user state persistence
embeds.py           — Discord embed builders
requirements.txt    — Python dependencies
```

---

## Setup

### 1. Create a Discord Application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. Under **Bot** → enable **Message Content Intent**, **Server Members Intent**, **Presence Intent**
3. Copy the **Bot Token** → set as `DISCORD_BOT_TOKEN` env var

### 2. Get a Football API Key

Register for free at [football-data.org](https://www.football-data.org/client/register) → copy your key → set as `FOOTBALL_DATA_API_KEY` env var

### 3. Invite the Bot

Generate an invite URL from the Discord portal with these permissions:
- Send Messages · Embed Links · Read Message History
- Add Reactions · Manage Messages · Use Slash Commands

### 4. Run

```bash
python bot.py
```

### 5. First-Time Server Setup

In your Discord server, run:
```
/setup
```
This walks you through setting every channel in one go. Then run `/checkapi` to verify the API connection is live.

---

## Command Reference

### ⚽ Match Commands

| Command | Description |
|---|---|
| `/today` | Today's World Cup matches |
| `/live` | Currently live matches |
| `/nextmatch` | Next upcoming match |
| `/upcoming` | Fixture list for the next 7 days |
| `/schedule` | Full match schedule (alias for `/upcoming`) |
| `/match <id>` | Full details for a specific match |
| `/team <name>` | National team profile + WC fixtures |
| `/standings` | All group tables |
| `/group <A–P>` | Single group table |
| `/worldcup` | Complete overview — all 16 groups |
| `/bracket` | Knockout bracket |
| `/matchcenter` | Live scores + next match + standings + leaderboard at once |
| `/scorers` | Top scorers leaderboard |
| `/dashboard` | Post the interactive live dashboard panel |
| `/help` | Paginated command browser |

---

### 🔍 Info

| Command | Description |
|---|---|
| `/player <name>` | Player profile — nationality, position, goals, assists, penalties |
| `/matchfacts <id>` | Full post-match breakdown — goals, cards, subs, lineups |
| `/tournamentstats` | Tournament dashboard — total goals, top scorers, tightest defences |

> **Note:** `/player` searches the live scorers list, so only players who have scored at least once appear. Use last name or first name if the full name isn't found.
>
> **Note:** `/matchfacts` requires a match ID. Get one from `/schedule`, `/today`, or `/match`.

---

### 🎯 Predictions

| Command | Description |
|---|---|
| `/predict <match>` | Predict the scoreline for an upcoming match |
| `/leaderboard` | All-time prediction leaderboard |
| `/monthlyleaderboard` | This month's prediction leaderboard |
| `/predstats` | Your personal prediction accuracy stats |

---

### 👤 Following

| Command | Description |
|---|---|
| `/followteam <name>` | Follow a national team for personal alerts |
| `/unfollowteam <name>` | Unfollow a team |
| `/myteams` | Your followed teams |

---

### ⚙️ Settings

| Command | Description |
|---|---|
| `/mysettings` | Toggle personal notifications (goals, cards, lineups, MOTM…) + set timezone |
| `/viewsettings` | Read-only view of your current settings |
| `/resetmysettings` | Reset all personal settings to server defaults |
| `/mytimezone` | Set your personal timezone — kickoff times shown in your local time |
| `/timezone` | Show server timezone + your personal timezone |

---

### 🔧 Admin

> Requires **Manage Channels** or **Administrator** permission.

| Command | Description |
|---|---|
| `/setup` | Interactive first-time setup wizard |
| `/status` | Bot config for this server |
| `/setchannel` | Live match alerts channel |
| `/setpredictionschannel` | Predictions & MOTM voting channel |
| `/setresultschannel` | Prediction results channel |
| `/setsummarychannel` | Match recaps & highlights channel |
| `/setcommandschannel` | Notification mode picker channel |
| `/setinteractivechannel` | User self-service panel channel |
| `/setmode <quiet\|standard\|detailed>` | Notification verbosity |
| `/settimezone <tz>` | Server timezone for daily summaries |
| `/resetleaderboard` | Wipe the prediction leaderboard |
| `/updatecommandsmenu` | Rebuild the notification mode picker |
| `/checkapi` | Diagnose the football-data.org connection |
| `/health` | Runtime health check — tasks, API, config |
| `/diagnostics` | Full system diagnostic |
| `/debug <match_id>` | Debug state for a specific match |
| `/reloadmatches` | Reload WC match order from the API |
| `/testnotifications <type>` | Send a test notification embed |
| `/testpredictions` | Test the prediction system end-to-end |
| `/testmotm` | Test MOTM voting without a live match |
| `/testembed <type>` | Preview any embed with realistic test data |

---

## Notification Channels

Configure up to 5 channels per server. Each serves a different purpose:

| Channel | What posts there |
|---|---|
| **Live Alerts** | Kickoff, goals, red cards, half-time, full-time |
| **Predictions** | Pre-match prediction prompts + MOTM votes |
| **Results** | Post-match prediction scoring + leaderboard update |
| **Summaries** | Daily schedule recap, match highlights link |
| **Commands** | Notification mode picker (quiet / standard / detailed) |

---

## Notification Modes

| Mode | What you get |
|---|---|
| `quiet` | Goals and final score only |
| `standard` | Goals, red cards, half-time, full-time *(default)* |
| `detailed` | Everything — lineups, subs, yellow cards, MOTM |

Set server-wide with `/setmode`. Users can override per-notification type via `/mysettings`.

---

## Personal Timezone

Each user can set their own timezone with `/mytimezone`. Kickoff times in ephemeral responses will be shown in your local time. The setting follows you across every server the bot is in.

---

## Architecture Notes

- All API calls go through `football_api.py`, which maintains a single `aiohttp` session and handles rate-limiting (429 back-off), auth errors, and timeouts gracefully.
- State (guild config, user settings, leaderboard, predictions) is persisted via `state.py`.
- Embeds are built in `embeds.py` — bot.py never constructs raw embed dicts, only calls embed builders.
- The bot is hardcoded to WC 2026 (`WC_CODE = "WC"`). No competition switching.
- Match numbering uses the chronological order of all WC 2026 fixtures (`Match 1`, `Match 2`, …) derived from the API at startup.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Commands don't appear | Wait up to 1 hour for global slash command sync, or kick & re-invite the bot |
| `401 / 403` on startup | Check `FOOTBALL_DATA_API_KEY` is set correctly — run `/checkapi` |
| No notifications posting | Run `/status` to check channels are configured; run `/testnotifications goal` to verify |
| Predictions not scoring | Run `/testpredictions` to step through the full flow |
| Wrong kickoff times | Run `/settimezone` (server) or `/mytimezone` (personal) |
| Bot crashes on import | Make sure `state.py` and `embeds.py` are in the same directory as `bot.py` |
