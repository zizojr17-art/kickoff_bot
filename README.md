# ⚽ FIFA World Cup 2026 — "Kickoff" Discord Bot

Your all-in-one companion bot for the **2026 FIFA World Cup** — live match alerts, score predictions, MOTM voting, leaderboards, standings, knockout bracket, and more.

---

## 🛠️ Bug-Fix Release — What Changed (25 Requirements)

This is the audited, bug-fixed version of Kickoff. All 25 issues from the spec review are resolved.

### Broadcasts & Mode Gates
| Req | Fix |
|-----|-----|
| #1 | Removed per-guild `mode` gate from live-match broadcasts — all guilds receive all events |
| #13 | Removed `_mode_at_least()` guards from all monitor-loop event triggers (reminder, lineup, poll, yellow/sub, 2H, ET, PSO) |

### Leaderboard & MOTM
| Req | Fix |
|-----|-----|
| #2 | End-of-day leaderboard post added to `daily_summary_loop`; guarded by per-date dedup key |
| #3 | `_build_motm_nominees` no longer falls back to team names — returns `[]` when no player data |
| #4 | `_process_fulltime` calls `get_match_motm()` for the official FIFA award winner; fallback to community tally |
| #5 | MOTM poll trigger corrected to `60 ≤ minute < 70` (was `≥ 70`) |
| #14 | Per-guild `state.mark_motm_result_sent` guard prevents duplicate MOTM result posts; results also go to match thread |
| #16 | Dead duplicate `_build_motm_nominees` function deleted |

### Embed Quality
| Req | Fix |
|-----|-----|
| #4/#14 | `embed_motm_result(is_official=True/False)` — distinct title/footer for official API award vs community vote |
| #6 | Fixed garbled UTF-8 mojibake in `_post_or_update_interactive_panel` (`âš½→⚽`, `â€"→—`, etc.) |

### Standings & Daily Summary
| Req | Fix |
|-----|-----|
| #9 | Knockout bracket only posted once at least one KO match has started/finished |
| #10 | All group tables pinned (not just last one); `save_pinned_standings_ids` / `get_pinned_standings_ids` track full list; `embed_all_groups_standings()` added to `embeds.py` |

### Match Status Handling
| Req | Fix |
|-----|-----|
| #21 | `SUSPENDED`, `CANCELLED`, `POSTPONED` statuses handled — one-time notification posted, no further events processed |

### Startup & Health
| Req | Fix |
|-----|-----|
| #20 | `on_ready` runs permission audit over all configured guild channels; warns on missing `Send Messages`, `Embed Links`, `Manage Messages`, `Create Public Threads`, `Send Messages in Threads` |
| #22 | `on_ready` logs configured timezone for every guild |

### Reliability
| Req | Fix |
|-----|-----|
| #7 | `_send_with_retry()` helper — exponential-backoff retry (3 attempts) on 429/5xx |
| #17 | `_get_or_create_match_thread` wraps `create_thread` in a 3-attempt retry loop |
| #19 | `_recent_sends: dict[str, float]` module-level dedup dict; key = `"{mid}:{event_key}"` → UTC timestamp |

### Slash Command Cleanup (req #25)
Deleted redundant/deprecated commands:
- `/dashboard` → use `/setup`
- `/schedule` → use `/upcoming`
- `/setresultschannel` → use `/setpredictionschannel`
- `/setmode` (slash + `!setmode` prefix) → use `/setup`
- `/updatecommandsmenu` → auto-managed
- `/debug` → use `/debugmatch`

### New Commands
| Req | Addition |
|-----|----------|
| #24 | `/testmatch step:[1–15]` — administrators step through every match lifecycle event (reminder → kickoff → goals → HT → FT → MOTM → recap) and verify each embed fires correctly |

### Other Fixes
| Req | Fix |
|-----|-----|
| #23 | `/testchannels` reads mode from `cfg.get("mode", "detailed")` instead of defunct `state.get_mode()` |

---

## 📁 Files

| File | Purpose |
|------|---------|
| `bot.py` | Main bot — all slash commands, monitor loop, daily summary loop, views |
| `football_api.py` | football-data.org API wrapper (async, flags, lineup helpers, score utils, `get_match_motm`) |
| `state.py` | JSON-backed persistent state — guild configs, predictions, leaderboard, pinned messages |
| `embeds.py` | All `discord.Embed` builders — one function per notification type |

---

## 🚀 Setup

### 1. Prerequisites
- Python 3.11+
- Discord bot token — [discord.com/developers](https://discord.com/developers/applications)
- football-data.org API key — [football-data.org](https://www.football-data.org/) (free tier works)

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Environment variables
```
DISCORD_TOKEN=your_discord_bot_token_here
FOOTBALL_DATA_API_KEY=your_football_data_org_key_here
SESSION_SECRET=any_random_secret_string
BOT_STATE_FILE=bot_state.json
```

### 4. Run
```bash
python bot.py
```

### 5. First-time server setup
Run `/setup` in your Discord server — the interactive wizard walks through every channel, timezone, and notification mode setting.

---

## ⚙️ Server Configuration (Admin Commands)

| Command | What it does |
|---------|-------------|
| `/setup` | Interactive setup wizard — recommended first step |
| `/setmatcheschannel` | Channel for live match alerts |
| `/setpredictionschannel` | Channel for prediction polls, MOTM votes, leaderboard |
| `/setsummarychannel` | Channel for daily recaps and EOD summary |
| `/settimezone` | Server timezone for daily summary timing |
| `/status` | Show current bot config for this server |

---

## 📋 Slash Commands

### Match Tracking
| Command | Description |
|---------|-------------|
| `/today` | Today's World Cup matches |
| `/live` | Currently live matches with scores |
| `/nextmatch` | Next upcoming match with countdown |
| `/upcoming [days]` | Fixtures for the next N days |
| `/match [match_id]` | Full details for a match |
| `/team [name]` | National team profile and fixtures |
| `/standings` | All group standings |
| `/group [A–P]` | Single group table |
| `/bracket` | Knockout bracket |
| `/scorers [limit]` | Top scorers list |

### Predictions & MOTM
| Command | Description |
|---------|-------------|
| `/predict [match_id]` | Predict the score |
| `/leaderboard` | All-time prediction leaderboard |
| `/predstats [user]` | Advanced stats — streak, exact scores, accuracy |

**Scoring:** +3 pts exact score · +1 pt correct result · +1 pt MOTM (when official)

---

## 🔔 Automatic Notifications

The monitor loop runs every **30 seconds**. All guilds receive all events in parallel.

| Event | When it fires |
|-------|---------------|
| 60-min reminder + early lineups | ~60 min before kick-off |
| 15-min reminder | ~15 min before kick-off |
| Prediction poll | ~90 min before kick-off |
| Kick-off | Status → `IN_PLAY` |
| Goal | Each new goal in match detail |
| Red card | Each red/yellow-red card |
| Half-time | Status → `PAUSED` |
| Second half | Status → `IN_PLAY` (from `PAUSED`) |
| Extra time | Status → `EXTRA_TIME` |
| Penalty shootout | Status → `PENALTY_SHOOTOUT` |
| MOTM vote | Minute 60–70 (in-play) |
| Full-time + prediction results | Status → `FINISHED` |
| Post-match recap | ~1 h after full-time |
| EOD summary + standings | 23:50 local time |
| Knockout bracket | EOD summary — only once KO matches have started |
| EOD leaderboard | EOD summary — predictions channel |

---

## 🧪 Admin Testing

### `/testembed`
Preview or broadcast any individual embed type with custom score/minute/teams.

### `/testmatch step:[1–15]`  *(new in this release)*
Step through a full match lifecycle one event at a time — sends to configured channels so you can verify every embed looks correct end-to-end.

Steps: `reminder_60 → reminder_15 → kickoff → lineup → goal → red_card → halftime → second_half → extra_time → penalty_shootout → fulltime → motm_open → motm_results → prediction_open → recap`

### `/testchannels`
Pings all configured channels and reports access status + current mode.

### `/debugmatch [match_id]`
Inspect full internal bot state for a specific match.

---

## 📌 Key state.py Additions (this release)

| Method | Purpose |
|--------|---------|
| `get_motm_result_sent(mid, gid)` / `mark_motm_result_sent(mid, gid)` | Per-guild MOTM result dedup |
| `get_pinned_standings_ids(gid)` / `save_pinned_standings_ids(gid, ids)` | Track all pinned group table messages |
| `is_leaderboard_eod_sent(date_key)` / `mark_leaderboard_eod_sent(date_key)` | EOD leaderboard dedup |

---

## 📦 Dependencies

```
discord.py>=2.4.0
aiohttp>=3.9.0
python-dotenv>=1.0.0
tzdata>=2024.1
```
