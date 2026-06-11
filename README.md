# World Cup 2026 "Kickoff" Discord Bot

A production-quality FIFA World Cup 2026 companion bot for Discord — live match alerts, score predictions, MOTM voting, group standings, interactive dashboard, and advanced prediction stats.

## Quick Start

1. Add secrets in the **Secrets** tab: `DISCORD_BOT_TOKEN` and `FOOTBALL_DATA_API_KEY`
2. Run `cd discord-bot && python bot.py`

## Stack

- Python 3.11, discord.py 2.4+, aiohttp 3.9+
- Data: football-data.org v4 API (World Cup competition only)
- State: JSON file (`discord-bot/bot_state.json`, or set `$BOT_STATE_FILE`)
- No database required

## Where things live

```
discord-bot/
├── bot.py          ← main bot: all slash commands, Views, background tasks
├── embeds.py       ← Discord embed builders (WC gold/blue theme)
├── state.py        ← JSON-backed state: predictions, leaderboard, guild config
├── football_api.py ← football-data.org API wrapper (WC-only)
├── requirements.txt
└── .env.example
```

## Slash Commands

| Category | Command | Description |
|---|---|---|
| Match | `/today` | Today's fixtures |
| Match | `/live` | Currently live matches |
| Match | `/nextmatch` | Next upcoming match |
| Match | `/standings` | Group stage tables |
| Match | `/group <A–P>` | Single group table (autocomplete) |
| Match | `/team <name>` | Team info + fixtures (autocomplete, 48 nations) |
| Match | `/upcoming` | Next 7 days of fixtures |
| Match | `/matchcenter` | Live score + next match + standings + leaderboard |
| Predictions | `/predict` | Submit/update your score prediction |
| Predictions | `/mypredictions` | View your open predictions |
| Predictions | `/leaderboard` | Prediction leaderboard |
| Predictions | `/predstats` | Your streak, accuracy, exact scores, monthly breakdown |
| Predictions | `/monthlyleaderboard` | Monthly prediction rankings |
| Following | `/followteam <name>` | Follow a national team |
| Following | `/unfollowteam <name>` | Unfollow a team (autocomplete from your list) |
| Following | `/myteams` | View/manage followed teams |
| Settings | `/mysettings` | Toggle personal notification preferences |
| Settings | `/viewsettings` | Read-only view of your settings |
| Settings | `/resetmysettings` | Reset to server defaults |
| Settings | `/timezone` | Show server timezone |
| Admin | `/setup` | Multi-step setup wizard (channels, timezone, mode) |
| Admin | `/dashboard` | Post persistent interactive dashboard panel |
| Admin | `/setchannel` | Set live alerts channel |
| Admin | `/setpredictionschannel` | Set predictions & MOTM channel |
| Admin | `/setresultschannel` | Set results channel |
| Admin | `/setsummarychannel` | Set recaps & highlights channel |
| Admin | `/setcommandschannel` | Set notification mode picker channel |
| Admin | `/setmode` | Guild verbosity: quiet / standard / detailed |
| Admin | `/settimezone` | Set server timezone (IANA format) |
| Admin | `/resetleaderboard` | Wipe the leaderboard |
| Admin | `/status` | Show bot config for this server |
| Admin | `/updatecommandsmenu` | Rebuild notification mode picker post |
| Help | `/help` | Auto-generated paginated command browser |

## Architecture

- **COMMAND_REGISTRY** — dict auto-populated at definition time via `_register()`; `/help` reads it at runtime so help never goes stale.
- **DashboardView** (`custom_id: dash_*`) — persistent View (timeout=None) with 7 buttons, registered in `on_ready` so it survives bot restarts.
- **InteractivePanel** — user self-service panel: notifications, followed teams, leaderboard, today's matches, prediction stats.
- **SetupWizardView** — multi-step wizard using ChannelSelect + Select Menus; no modals needed.
- **World Cup only** — all API calls target the `WC` competition code; non-WC data is rejected at the API layer.
- **Autocomplete** — WC_TEAMS (48 nations) and WC_GROUPS (A–P) in `football_api.py` power team/group inputs.
- **Prediction scoring** — exact score = 3 pts, correct result = 1 pt, MOTM correct pick = 1 pt; stats tracked per-user with streak, monthly breakdown.
- **Monitor loop** — runs every 2 minutes; detects goals, red cards, HT, 2H, ET, PSO, MOTM trigger (~75 min), FT.

## Permissions required

Bot needs: **Send Messages**, **Embed Links**, **Manage Messages**, **Read Message History**

## Gotchas

- Slash commands take up to 1 hour to propagate globally after first sync
- `on_ready` fires again on reconnect — background loops are guarded against double-start
- football-data.org free tier: 10 req/min — monitor loop runs every 2 minutes to stay within limits
- `DISCORD_BOT_TOKEN` must never be committed; keep it in Replit Secrets only

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._
