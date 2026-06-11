# ⚽ FIFA World Cup 2026 "Kickoff" Discord Bot

Your all-in-one companion bot for the **2026 FIFA World Cup** — live alerts, score predictions, leaderboards, player stats, MOTM voting, and more.

---

## 📁 Files

| File | Purpose |
|------|---------|
| `bot.py` | Main bot — all commands, background monitor, views, autocomplete |
| `football_api.py` | football-data.org API wrapper (caching, helpers, WC match order) |
| `state.py` | Persistent state — guild configs, predictions, leaderboard, MOTM |
| `embeds.py` | All `discord.Embed` builders — one function per notification type |
| `requirements.txt` | Python dependencies |

---

## 🚀 Setup

### 1. Prerequisites
- Python 3.12+
- A Discord bot token (from [discord.com/developers](https://discord.com/developers/applications))
- A [football-data.org](https://www.football-data.org/) API key (free tier works)

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Environment variables
```bash
DISCORD_BOT_TOKEN=your_discord_token_here
FOOTBALL_API_KEY=your_football_data_org_key_here
```

### 4. Run
```bash
python bot.py
```

### 5. First-time server setup
In your Discord server, run `/setup` — the interactive wizard walks through every channel and mode setting.

---

## ⚙️ Server Configuration (Admin commands)

| Command | What it does |
|---------|-------------|
| `/setup` | Interactive setup wizard — recommended first step |
| `/setmatcheschannel` | Channel for live match alerts (goals, cards, kick-off, FT) |
| `/setpredictionschannel` | Channel for prediction polls and MOTM votes |
| `/setresultschannel` | Channel for prediction results and scoreboards |
| `/setsummarychannel` | Channel for daily recaps and highlight links |
| `/setcommandschannel` | Post the notification-mode picker panel |
| `/setinteractivechannel` | Post the self-service user panel |
| `/setmode` | Verbosity: `quiet` / `standard` / `detailed` |
| `/settimezone` | Server timezone for daily summary timing |
| `/status` | Show current bot config for this server |

---

## 📋 Slash Commands

### Match Commands
| Command | Description |
|---------|-------------|
| `/today` | Today's World Cup matches |
| `/live` | Currently live matches with scores |
| `/nextmatch` | Next upcoming match with countdown |
| `/upcoming [days]` | Fixtures for the next N days (default 7) |
| `/schedule [days]` | Alias for `/upcoming` |
| `/match [match_id]` | Full details for a match — **autocomplete** picks from upcoming matches |
| `/team [name]` | National team profile and fixtures — **autocomplete with flags** |
| `/standings` | All group standings |
| `/group [A–P]` | Single group table — **autocomplete** |
| `/bracket` | Knockout bracket |
| `/worldcup` | Full tournament overview (all groups) |
| `/matchcenter` | Live + next match + standings + leaderboard in one view |
| `/scorers [limit]` | Top scorers list |
| `/help` | Paginated command browser by category |
| `/dashboard` | Post the interactive match dashboard panel |

### Predictions
| Command | Description |
|---------|-------------|
| `/predict [match_id]` | Predict the score — **autocomplete** shows upcoming matches with flags |
| `/leaderboard` | All-time prediction leaderboard for this server |
| `/monthlyleaderboard [month]` | Monthly leaderboard (YYYY-MM format) |
| `/predstats [user]` | Advanced stats — streak, exact scores, accuracy |

**Scoring:** +3 pts for exact score, +1 pt for correct result.

### Following
| Command | Description |
|---------|-------------|
| `/followteam [name]` | Follow a team — autocomplete shows all 48 nations with flags |
| `/unfollowteam [name]` | Unfollow — autocomplete shows your followed teams |
| `/myteams` | View and manage your followed teams |

### Personal Settings
| Command | Description |
|---------|-------------|
| `/mysettings` | Toggle personal notification preferences |
| `/viewsettings` | View your current settings (read-only) |
| `/resetmysettings` | Reset personal settings to server defaults |
| `/mytimezone [tz]` | Set your timezone for match times — autocomplete |
| `/timezone` | View this server's timezone |

### Info
| Command | Description |
|---------|-------------|
| `/player [name]` | Player's WC 2026 stats (goals, assists, penalties) |
| `/matchfacts [match_id]` | Goals, cards & full facts — **autocomplete** picks the match |
| `/tournamentstats` | Tournament-wide statistics dashboard |
| `/countdown` | Time remaining until kick-off (or days since it started) |
| `/about` | Bot info, version, server count |
| `/ping` | Latency and API response time |

### Admin / Testing
| Command | Description |
|---------|-------------|
| `/testnotifications` | Send a test notification to all configured channels |
| `/testpredictions [action]` | Test prediction system end-to-end (open/status/resolve/clear) |
| `/testmotm [action]` | Test MOTM voting workflow (open/status/results/clear) |
| `/testembed [type]` | Preview any notification embed with realistic test data |
| `/checkapi` | Diagnose the football-data.org API connection |
| `/health` | Runtime health check — tasks, API, config |
| `/diagnostics` | Full system diagnostic — API, state, tasks |
| `/debug [match_id]` | Debug state for a specific match |
| `/reloadmatches` | Force-reload the WC match order cache |
| `/resetleaderboard` | Reset the server leaderboard (irreversible) |

---

## 🔔 Automatic Notifications

The monitor loop runs every **60 seconds**. Notifications are sent to **all guilds in parallel** so every server receives alerts at the same time.

| Event | Mode required | Timing |
|-------|--------------|--------|
| 60-min reminder | standard | ~60 min before kick-off |
| 15-min reminder | standard | ~15 min before kick-off |
| Kick-off | quiet | When status → IN_PLAY |
| Confirmed lineups | detailed | With kick-off (if available) |
| Goal | quiet | Each new goal event |
| Red card | quiet | Each red card |
| Half-time | quiet | When status → PAUSED |
| Second half | detailed | When status → IN_PLAY (from PAUSED) |
| Extra time | standard | When status → EXTRA_TIME |
| Penalty shootout | standard | When status → PENALTY_SHOOTOUT |
| MOTM vote | standard | When minute ≥ 75 (IN_PLAY) |
| Full-time + results | quiet | When status → FINISHED |
| Daily summary | detailed | Server's configured time |

### Notification modes
- **quiet** — kick-off, goals, red cards, full-time only
- **standard** — quiet + reminders, MOTM vote, halftime, extra-time
- **detailed** — standard + lineups, second half, recaps, daily summary

---

## 🏆 Prediction System

1. A prediction poll is posted automatically ~90 min before kick-off
2. Users click **🎯 Predict Score** and use the +/− buttons — team flags shown on each button
3. Predictions lock automatically when the match kicks off
4. After full-time: exact scores earn **+3 pts**, correct result **+1 pt**
5. Results, leaderboard update, and winners are announced automatically

---

## 🗳️ Man of the Match (MOTM)

- Voting opens automatically at **minute 75** of any live match
- Nominees are drawn from goal scorers and starting XI
- Users pick one player from a dropdown
- Winner is announced at full-time with leaderboard points awarded

---

## 🌍 Timezone Support

- `/mytimezone` — each user sets their own timezone (used in `/predict` autocomplete kick-off times)
- `/settimezone` — server-wide timezone for daily summary scheduling
- All kick-off times in embeds should use `<t:{kickoffTs}:F>` (full) / `<t:{kickoffTs}:R>` (countdown) — these render in **each viewer's local timezone** automatically via Discord

---

## 🛠️ Technical Notes

### v2.1 Changes (bot.py)
- Monitor loop: `@tasks.loop(minutes=2)` → `@tasks.loop(seconds=60)`
- `broadcast()`, `broadcast_predictions()`, `broadcast_results()`, `broadcast_summary()` — all now use `asyncio.gather()` instead of sequential `await`
- Reminder window widened from `< 1.5` to `< 2.0` minutes
- MOTM check moved outside `if status_changed:` block (was a silent bug)
- `autocomplete_match_id()` added — shared by `/predict`, `/match`, `/matchfacts`
- `autocomplete_team()` and `autocomplete_followed_team()` now include flag emoji in choice names
- `ScoreInputView` stores `home_flag` / `away_flag`, shown in `_content()` and button labels
- `_annotate_match()` now injects `kickoffTs` (Unix timestamp) for TZ-correct Discord timestamps
- `_monitor_tick` counter added — reloads WC match order every 60 loop iterations (≈ hourly)
- `/about` embed updated to reflect 60 s polling interval

### embeds.py — recommended update
To fully fix the timezone countdown issue, update time display in embeds to use Discord timestamps:
```python
# Instead of manually computing "Kick-off in X hours":
f"<t:{match['kickoffTs']}:R>"   # "in 2 hours" — auto TZ, live countdown
f"<t:{match['kickoffTs']}:F>"   # "Wednesday, 18 June 2026 20:00" — full date/time in user's TZ
f"<t:{match['kickoffTs']}:t>"   # "20:00" — time only in user's TZ
```

---

## 📦 Dependencies

```
discord.py>=2.4.0
aiohttp>=3.9.0
tzdata>=2024.1
```
