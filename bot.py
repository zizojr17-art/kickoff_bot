"""
Kickoff Bot — World Cup & football match notifications for Discord.

Slash (/) commands are the primary interface (no special intent needed).
Prefix (!) commands require "Message Content" Privileged Intent in the dev portal.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import state
import embeds as emb
from football_api import (
    get_todays_matches,
    get_match_detail,
    get_live_matches,
    get_standings,
    get_competition_matches,
    get_next_match,
    get_team,
    get_team_matches,
    search_team,
    load_wc_match_order,
    parse_dt,
    get_current_score,
    goal_key,
    card_key,
    COMPETITION_CODES,
    STAGE_NAMES,
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

# ── Bot setup ─────────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

intents = discord.Intents.default()
# To also use prefix (!) commands in servers, enable "Message Content Intent"
# at discord.com/developers/applications → Bot → Privileged Gateway Intents.

bot  = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

state.load()

# Force-refresh counter — every 6 loops (≈12 min) we fetch detail for all live
# matches even if score/status look unchanged, to catch red cards mid-game.
_force_counter: int = 0


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def broadcast(embed: discord.Embed, reactions: list[str] | None = None) -> None:
    """Send an embed to every configured notification channel."""
    for gid, cfg in state.all_guild_configs().items():
        cid = cfg.get("channel_id")
        if not cid:
            continue
        channel = bot.get_channel(int(cid))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            continue
        try:
            msg = await channel.send(embed=embed)
            if reactions:
                for r in reactions:
                    await msg.add_reaction(r)
        except discord.Forbidden:
            log.warning("[ERROR] No permission to send to channel %s (guild %s)", cid, gid)
        except discord.HTTPException as e:
            log.error("[ERROR] HTTPException sending to %s: %s", cid, e)


def any_channel_configured() -> bool:
    return any(v.get("channel_id") for v in state.all_guild_configs().values())


def _tz_for(guild_id: int | str) -> ZoneInfo:
    cfg = state.get_guild_config(str(guild_id))
    tz_str = cfg.get("timezone", "UTC")
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


async def _safe_defer(interaction: discord.Interaction) -> None:
    try:
        await interaction.response.defer(thinking=True)
    except discord.HTTPException:
        pass


async def _send_embeds(target, embed_list: list[discord.Embed]) -> None:
    for i, em in enumerate(embed_list):
        if isinstance(target, discord.Interaction):
            await target.followup.send(embed=em)
        else:
            await target.send(embed=em)


# ══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND LOOPS
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(minutes=2)
async def monitor_loop() -> None:
    global _force_counter
    _force_counter += 1
    force_refresh = (_force_counter % 6 == 0)   # full detail sweep every ~12 min

    if not any_channel_configured():
        return

    try:
        matches = await get_todays_matches()   # single bulk API call
    except Exception as e:
        log.error("[ERROR] monitor_loop fetch failed: %s", e)
        return

    now = datetime.now(timezone.utc)

    for match in matches:
        mid    = str(match["id"])
        status = match.get("status", "")
        ko_dt  = parse_dt(match.get("utcDate"))
        sent   = state.get_sent(mid)

        # ── Pre-match reminders ────────────────────────────────────────────
        if status in ("SCHEDULED", "TIMED") and ko_dt:
            mins = (ko_dt - now).total_seconds() / 60

            if 85 <= mins <= 95 and "poll" not in sent:
                state.mark_sent(mid, "poll")
                log.info("[MATCH] Poll posted for match %s", mid)
                await broadcast(emb.embed_prediction_poll(match), reactions=["1️⃣", "🤝", "2️⃣"])

            if 55 <= mins <= 70 and "60m" not in sent:
                state.mark_sent(mid, "60m")
                log.info("[MATCH] 60-min reminder for match %s", mid)
                await broadcast(emb.embed_reminder(match, 60))

            if 10 <= mins <= 20 and "15m" not in sent:
                state.mark_sent(mid, "15m")
                log.info("[MATCH] 15-min reminder for match %s", mid)
                await broadcast(emb.embed_reminder(match, 15))

        # ── Kick-off ───────────────────────────────────────────────────────
        if status == "IN_PLAY" and "kickoff" not in sent:
            state.mark_sent(mid, "kickoff")
            log.info("[MATCH] Kick-off for match %s", mid)
            await broadcast(emb.embed_kickoff(match))

        # ── Live event detection (score / status diff-based) ──────────────
        if status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
            snap       = state.get_snapshot(mid)
            h, a       = get_current_score(match)
            prev_h     = snap.get("home_score")
            prev_a     = snap.get("away_score")
            prev_status = snap.get("status", "")

            score_changed  = (h is not None) and (h != prev_h or a != prev_a)
            status_changed = status != prev_status

            if score_changed or status_changed or force_refresh:
                await _process_live_detail(match, snap, score_changed, status_changed)
            elif not snap:
                # First time we see this match live — record baseline without API call
                state.set_snapshot(mid, {"home_score": h, "away_score": a, "status": status})

        # ── Full-time ─────────────────────────────────────────────────────
        if status == "FINISHED" and "FT" not in sent:
            state.mark_sent(mid, "FT")
            detail = await get_match_detail(match["id"])
            log.info("[MATCH] FT for match %s", mid)
            await broadcast(emb.embed_fulltime(match, detail))


async def _process_live_detail(
    match: dict,
    snap: dict,
    score_changed: bool,
    status_changed: bool,
) -> None:
    """Fetch detail for a single live match and fire events for any changes."""
    mid    = str(match["id"])
    detail = await get_match_detail(match["id"])
    if not detail:
        return

    goals     = detail.get("goals") or []
    bookings  = detail.get("bookings") or []
    red_cards = [b for b in bookings if b.get("card") in ("RED", "YELLOW_RED")]
    cur_status = match.get("status", "")
    h, a       = get_current_score(match)

    # ── Goals ──────────────────────────────────────────────────────────────
    announced_goals = state.get_announced_goals(mid)
    for goal in goals:
        gk = goal_key(goal)
        if gk not in announced_goals:
            state.announce_goal(mid, gk)
            scorer = (goal.get("scorer") or {}).get("name", "")
            minute = goal.get("minute", "?")
            log.info("[GOAL] Match %s — %s' %s", mid, minute, scorer or "unknown")
            await broadcast(emb.embed_goal(match, detail, goal))

    # ── Red cards ──────────────────────────────────────────────────────────
    announced_cards = state.get_announced_cards(mid)
    for card in red_cards:
        ck = card_key(card)
        if ck not in announced_cards:
            state.announce_card(mid, ck)
            player = (card.get("player") or {}).get("name", "unknown")
            minute = card.get("minute", "?")
            log.info("[CARD] Match %s — %s' %s (%s)", mid, minute, player, card.get("card"))
            await broadcast(emb.embed_red_card(match, detail, card))

    # ── Status transitions ─────────────────────────────────────────────────
    sent        = state.get_sent(mid)
    prev_status = snap.get("status", "")
    configs     = state.all_guild_configs()

    if status_changed:
        if cur_status == "PAUSED" and "HT" not in sent:
            state.mark_sent(mid, "HT")
            log.info("[MATCH] Half-time for match %s", mid)
            await broadcast(emb.embed_halftime(match, detail))

        elif cur_status == "IN_PLAY" and prev_status == "PAUSED" and "2H" not in sent:
            state.mark_sent(mid, "2H")
            log.info("[MATCH] Second half for match %s", mid)
            # Detailed mode only
            for gid, cfg in configs.items():
                if cfg.get("mode", "quiet") == "detailed":
                    cid = cfg.get("channel_id")
                    if cid:
                        ch = bot.get_channel(int(cid))
                        if ch:
                            try:
                                await ch.send(embed=emb.embed_second_half(match))
                            except discord.HTTPException:
                                pass

        elif cur_status == "EXTRA_TIME" and "ET" not in sent:
            state.mark_sent(mid, "ET")
            log.info("[MATCH] Extra time for match %s", mid)
            await broadcast(emb.embed_extra_time(match, detail))

        elif cur_status == "PENALTY_SHOOTOUT" and "PSO" not in sent:
            state.mark_sent(mid, "PSO")
            log.info("[MATCH] Penalty shootout for match %s", mid)
            await broadcast(emb.embed_penalty_shootout(match, detail))

    # ── Update snapshot ────────────────────────────────────────────────────
    state.set_snapshot(mid, {"home_score": h, "away_score": a, "status": cur_status})


@monitor_loop.before_loop
async def _before_monitor():
    await bot.wait_until_ready()
    await asyncio.sleep(3)


@tasks.loop(minutes=5)
async def daily_summary_loop() -> None:
    """Send daily summary at 23:50–23:59 in each server's configured timezone."""
    configs = state.all_guild_configs()
    if not configs:
        return

    try:
        matches = await get_todays_matches()
    except Exception as e:
        log.error("[SUMMARY] Failed to fetch matches: %s", e)
        return

    for gid, cfg in configs.items():
        cid = cfg.get("channel_id")
        if not cid:
            continue

        tz = _tz_for(gid)
        local_now = datetime.now(tz)

        if local_now.hour != 23 or local_now.minute < 50:
            continue

        date_key = f"{gid}_{local_now.strftime('%Y-%m-%d')}"
        if state.is_daily_summary_sent(date_key):
            continue

        channel = bot.get_channel(int(cid))
        if not channel:
            continue

        date_str = local_now.strftime("%d %b %Y")
        try:
            await channel.send(embed=emb.embed_daily_summary(matches, date_str))
            state.mark_daily_summary_sent(date_key)
            log.info("[SUMMARY] Daily summary sent to guild %s (%s)", gid, date_str)
        except discord.HTTPException as e:
            log.error("[SUMMARY] Failed to send to guild %s: %s", gid, e)


@daily_summary_loop.before_loop
async def _before_daily():
    await bot.wait_until_ready()


# ══════════════════════════════════════════════════════════════════════════════
#  ON_READY
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready() -> None:
    log.info("[MATCH] Logged in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        synced = await tree.sync()
        log.info("[MATCH] Synced %d slash commands", len(synced))
    except Exception as e:
        log.error("[ERROR] Slash sync failed: %s", e)

    await load_wc_match_order()

    monitor_loop.start()
    daily_summary_loop.start()

    await broadcast(emb.embed_startup(bot.user))
    log.info("[MATCH] Startup complete — %d guilds configured", len(state.all_guild_configs()))


# ══════════════════════════════════════════════════════════════════════════════
#  COMMANDS  —  slash + prefix pairs
# ══════════════════════════════════════════════════════════════════════════════

# ── /today  !today ────────────────────────────────────────────────────────────

@tree.command(name="today", description="Show today's football matches")
async def slash_today(interaction: discord.Interaction):
    await _safe_defer(interaction)
    matches = await get_todays_matches()
    await _send_embeds(interaction, emb.embed_today(matches))


@bot.command(name="today", help="Show today's football matches")
async def prefix_today(ctx):
    async with ctx.typing():
        matches = await get_todays_matches()
    for em in emb.embed_today(matches):
        await ctx.send(embed=em)


# ── /matchtoday  (alias with optional competition filter) ────────────────────

@tree.command(name="matchtoday", description="Today's matches, optionally filtered by competition")
@app_commands.describe(competition="Competition code e.g. WC, CL, PL — leave blank for all")
async def slash_matchtoday(interaction: discord.Interaction, competition: str = ""):
    await _safe_defer(interaction)
    matches = await get_todays_matches()
    if competition:
        code = competition.upper()
        matches = [m for m in matches if m.get("competition", {}).get("code") == code]
    await _send_embeds(interaction, emb.embed_today(matches))


# ── /live  !live ──────────────────────────────────────────────────────────────

@tree.command(name="live", description="Show currently live matches")
async def slash_live(interaction: discord.Interaction):
    await _safe_defer(interaction)
    matches = await get_live_matches()
    await interaction.followup.send(embed=emb.embed_live(matches))


@bot.command(name="live", help="Show currently live matches")
async def prefix_live(ctx):
    async with ctx.typing():
        matches = await get_live_matches()
    await ctx.send(embed=emb.embed_live(matches))


# ── /nextmatch  !nextmatch ────────────────────────────────────────────────────

@tree.command(name="nextmatch", description="Show the next upcoming match")
@app_commands.describe(competition="Competition code e.g. WC — leave blank for any")
async def slash_nextmatch(interaction: discord.Interaction, competition: str = ""):
    await _safe_defer(interaction)
    match = await get_next_match(competition or None)
    if not match:
        await interaction.followup.send(
            embed=discord.Embed(description="📭 No upcoming matches found in the next 3 days.", color=emb.C_GREY)
        )
        return
    await interaction.followup.send(embed=emb.embed_nextmatch(match))


@bot.command(name="nextmatch", help="Show the next upcoming match")
async def prefix_nextmatch(ctx, competition: str = ""):
    async with ctx.typing():
        match = await get_next_match(competition or None)
    if not match:
        await ctx.send(embed=discord.Embed(description="📭 No upcoming matches in the next 3 days.", color=emb.C_GREY))
        return
    await ctx.send(embed=emb.embed_nextmatch(match))


# ── /standings  !standings ────────────────────────────────────────────────────

@tree.command(name="standings", description="Show competition standings")
@app_commands.describe(competition="Competition code e.g. WC, CL, PL")
async def slash_standings(interaction: discord.Interaction, competition: str = "WC"):
    await _safe_defer(interaction)
    data = await get_standings(competition.upper())
    if not data:
        codes = ", ".join(COMPETITION_CODES.keys())
        await interaction.followup.send(
            embed=discord.Embed(description=f"❌ No data for `{competition.upper()}`. Try: {codes}", color=emb.C_RED)
        )
        return
    await _send_embeds(interaction, emb.embed_standings(data))


@bot.command(name="standings", help="Show standings. Usage: !standings WC")
async def prefix_standings(ctx, competition: str = "WC"):
    async with ctx.typing():
        data = await get_standings(competition.upper())
    if not data:
        await ctx.send(embed=discord.Embed(description=f"❌ No data for `{competition.upper()}`.", color=emb.C_RED))
        return
    for em in emb.embed_standings(data):
        await ctx.send(embed=em)


# ── /upcoming  !upcoming ──────────────────────────────────────────────────────

@tree.command(name="upcoming", description="Upcoming matches for a competition")
@app_commands.describe(competition="Competition code e.g. WC", days="Days ahead (1–30)")
async def slash_upcoming(interaction: discord.Interaction, competition: str = "WC", days: int = 7):
    await _safe_defer(interaction)
    code  = competition.upper()
    days  = max(1, min(days, 30))
    today = datetime.now(timezone.utc)
    matches = await get_competition_matches(
        code,
        today.strftime("%Y-%m-%d"),
        (today + timedelta(days=days)).strftime("%Y-%m-%d"),
    )
    await interaction.followup.send(embed=emb.embed_upcoming(matches, code, days))


@bot.command(name="upcoming", help="Upcoming matches. Usage: !upcoming WC 7")
async def prefix_upcoming(ctx, competition: str = "WC", days: int = 7):
    code  = competition.upper()
    days  = max(1, min(days, 30))
    today = datetime.now(timezone.utc)
    async with ctx.typing():
        matches = await get_competition_matches(
            code,
            today.strftime("%Y-%m-%d"),
            (today + timedelta(days=days)).strftime("%Y-%m-%d"),
        )
    await ctx.send(embed=emb.embed_upcoming(matches, code, days))


# ── /team  !team ──────────────────────────────────────────────────────────────

@tree.command(name="team", description="Show team profile and fixtures")
@app_commands.describe(name="Team name e.g. Brazil, Germany, USA")
async def slash_team(interaction: discord.Interaction, name: str):
    await _safe_defer(interaction)
    team = await search_team(name)
    if not team:
        await interaction.followup.send(
            embed=discord.Embed(description=f"❌ Team `{name}` not found. Try a shorter name.", color=emb.C_RED)
        )
        return
    tid = team["id"]
    recent, upcoming = await asyncio.gather(
        get_team_matches(tid, status="FINISHED", limit=5),
        get_team_matches(tid, status="SCHEDULED", limit=5),
    )
    await interaction.followup.send(embed=emb.embed_team(team, recent, upcoming))


@bot.command(name="team", help="Show team profile. Usage: !team Brazil")
async def prefix_team(ctx, *, name: str):
    async with ctx.typing():
        team = await search_team(name)
    if not team:
        await ctx.send(embed=discord.Embed(description=f"❌ Team `{name}` not found.", color=emb.C_RED))
        return
    tid = team["id"]
    recent, upcoming = await asyncio.gather(
        get_team_matches(tid, status="FINISHED", limit=5),
        get_team_matches(tid, status="SCHEDULED", limit=5),
    )
    await ctx.send(embed=emb.embed_team(team, recent, upcoming))


# ── /group  !group  (WC group stage) ─────────────────────────────────────────

def _find_group_block(tables: list[dict], letter: str) -> dict | None:
    """
    Locate a group block by letter, trying multiple key formats:
    GROUP_A  →  A  →  suffix match  (handles GROUP_A through GROUP_P).
    """
    target = letter.strip().upper()
    # Primary format used by football-data.org
    exact = next((t for t in tables if t.get("group") == f"GROUP_{target}"), None)
    if exact:
        return exact
    # Fallback: group field ends with the target letter
    suffix = next((t for t in tables if (t.get("group") or "").upper().endswith(target)), None)
    if suffix:
        return suffix
    # Fallback: group field IS just the letter
    return next((t for t in tables if (t.get("group") or "").upper() == target), None)


@tree.command(name="group", description="World Cup group table — e.g. /group A  (groups A–P in WC 2026)")
@app_commands.describe(letter="Group letter A–P")
async def slash_group(interaction: discord.Interaction, letter: str):
    await _safe_defer(interaction)
    data = await get_standings("WC")
    if not data:
        await interaction.followup.send(
            embed=discord.Embed(
                description=(
                    "❌ World Cup standings are not available yet.\n"
                    "They will appear once the group stage begins."
                ),
                color=emb.C_RED,
            )
        )
        return
    tables = data.get("standings", [])
    block  = _find_group_block(tables, letter)
    if not block:
        available = ", ".join(
            f"`{t.get('group','').replace('GROUP_','')}`"
            for t in tables if t.get("group")
        ) or "none yet"
        await interaction.followup.send(
            embed=discord.Embed(
                description=(
                    f"❌ Group **{letter.upper()}** not found.\n"
                    f"Available groups: {available}"
                ),
                color=emb.C_RED,
            )
        )
        return
    actual_letter = (block.get("group") or letter).replace("GROUP_", "")
    await interaction.followup.send(embed=emb.embed_wc_group(actual_letter, block.get("table", [])))


@bot.command(name="group", help="WC group table. Usage: !group A")
async def prefix_group(ctx, letter: str = "A"):
    async with ctx.typing():
        data = await get_standings("WC")
    if not data:
        await ctx.send(embed=discord.Embed(
            description="❌ World Cup standings not available yet.", color=emb.C_RED
        ))
        return
    tables = data.get("standings", [])
    block  = _find_group_block(tables, letter)
    if not block:
        available = ", ".join(
            f"`{t.get('group','').replace('GROUP_','')}`"
            for t in tables if t.get("group")
        ) or "none yet"
        await ctx.send(embed=discord.Embed(
            description=f"❌ Group **{letter.upper()}** not found.\nAvailable: {available}",
            color=emb.C_RED,
        ))
        return
    actual_letter = (block.get("group") or letter).replace("GROUP_", "")
    await ctx.send(embed=emb.embed_wc_group(actual_letter, block.get("table", [])))


# ── /worldcup  !worldcup ──────────────────────────────────────────────────────

@tree.command(name="worldcup", description="FIFA World Cup 2026 overview — all groups")
async def slash_worldcup(interaction: discord.Interaction):
    await _safe_defer(interaction)
    data = await get_standings("WC")
    if not data:
        await interaction.followup.send(
            embed=discord.Embed(description="❌ World Cup data not available yet.", color=emb.C_RED)
        )
        return
    await _send_embeds(interaction, emb.embed_worldcup_overview(data))


@bot.command(name="worldcup", help="World Cup 2026 overview — all groups")
async def prefix_worldcup(ctx):
    async with ctx.typing():
        data = await get_standings("WC")
    if not data:
        await ctx.send(embed=discord.Embed(description="❌ World Cup data not available yet.", color=emb.C_RED))
        return
    for em in emb.embed_worldcup_overview(data):
        await ctx.send(embed=em)


# ── /bracket  !bracket ────────────────────────────────────────────────────────

@tree.command(name="bracket", description="FIFA World Cup 2026 knockout bracket")
async def slash_bracket(interaction: discord.Interaction):
    await _safe_defer(interaction)
    today = datetime.now(timezone.utc)
    matches = await get_competition_matches(
        "WC",
        "2026-01-01",
        (today + timedelta(days=60)).strftime("%Y-%m-%d"),
    )
    knockout = [
        m for m in matches
        if m.get("stage") in ("LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")
    ]
    await _send_embeds(interaction, emb.embed_bracket(knockout))


@bot.command(name="bracket", help="World Cup knockout bracket")
async def prefix_bracket(ctx):
    today = datetime.now(timezone.utc)
    async with ctx.typing():
        matches = await get_competition_matches(
            "WC", "2026-01-01", (today + timedelta(days=60)).strftime("%Y-%m-%d")
        )
    knockout = [m for m in matches if m.get("stage") in
                ("LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")]
    for em in emb.embed_bracket(knockout):
        await ctx.send(embed=em)


# ── /match  !match  (by match ID) ─────────────────────────────────────────────

@tree.command(name="match", description="Show details for a specific match by ID")
@app_commands.describe(match_id="Match ID from football-data.org")
async def slash_match(interaction: discord.Interaction, match_id: int):
    await _safe_defer(interaction)
    detail = await get_match_detail(match_id)
    if not detail:
        await interaction.followup.send(
            embed=discord.Embed(description=f"❌ Match `{match_id}` not found.", color=emb.C_RED)
        )
        return
    status = detail.get("status", "")
    if status == "FINISHED":
        await interaction.followup.send(embed=emb.embed_fulltime(detail, detail))
    elif status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
        await interaction.followup.send(embed=emb.embed_live([detail]))
    else:
        await interaction.followup.send(embed=emb.embed_nextmatch(detail))


@bot.command(name="match", help="Match details by ID. Usage: !match 12345")
async def prefix_match(ctx, match_id: int):
    async with ctx.typing():
        detail = await get_match_detail(match_id)
    if not detail:
        await ctx.send(embed=discord.Embed(description=f"❌ Match `{match_id}` not found.", color=emb.C_RED))
        return
    status = detail.get("status", "")
    if status == "FINISHED":
        await ctx.send(embed=emb.embed_fulltime(detail, detail))
    else:
        await ctx.send(embed=emb.embed_nextmatch(detail))


# ── /competitions  !competitions ──────────────────────────────────────────────

@tree.command(name="competitions", description="List supported competition codes")
async def slash_competitions(interaction: discord.Interaction):
    await interaction.response.send_message(embed=emb.embed_competitions())


@bot.command(name="competitions", help="List supported competition codes")
async def prefix_competitions(ctx):
    await ctx.send(embed=emb.embed_competitions())


# ── /setchannel  !setchannel ──────────────────────────────────────────────────

@tree.command(name="setchannel", description="Set this channel for automatic match notifications")
@app_commands.default_permissions(manage_channels=True)
async def slash_setchannel(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    cfg["channel_id"] = str(interaction.channel_id)
    state.set_guild_config(gid, cfg)
    log.info("[MATCH] Notification channel set for guild %s → %s", gid, interaction.channel_id)
    await interaction.response.send_message(embed=discord.Embed(
        description=(
            f"✅ **#{interaction.channel.name}** is now the match notification channel.\n"
            "Use `/setmode` to switch between **quiet** and **detailed** notifications."
        ),
        color=emb.C_GREEN,
    ))


@bot.command(name="setchannel", help="Set this channel for automatic notifications")
@commands.has_permissions(manage_channels=True)
async def prefix_setchannel(ctx):
    gid = str(ctx.guild.id)
    cfg = state.get_guild_config(gid)
    cfg["channel_id"] = str(ctx.channel.id)
    state.set_guild_config(gid, cfg)
    await ctx.send(embed=discord.Embed(
        description=f"✅ **#{ctx.channel.name}** set as notification channel.",
        color=emb.C_GREEN,
    ))


# ── /setmode  !setmode ────────────────────────────────────────────────────────

@tree.command(name="setmode", description="Set notification verbosity")
@app_commands.describe(mode="quiet = key events only | detailed = all events")
@app_commands.choices(mode=[
    app_commands.Choice(name="quiet (default) — goals, red cards, halftime, FT", value="quiet"),
    app_commands.Choice(name="detailed — all events + second half + ET + polls", value="detailed"),
])
@app_commands.default_permissions(manage_channels=True)
async def slash_setmode(interaction: discord.Interaction, mode: str = "quiet"):
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    cfg["mode"] = mode
    state.set_guild_config(gid, cfg)
    icon = "🔇" if mode == "quiet" else "📢"
    await interaction.response.send_message(embed=discord.Embed(
        description=f"{icon} Notification mode set to **{mode}**.", color=emb.C_GREEN
    ))


@bot.command(name="setmode", help="Set notification mode: quiet or detailed")
@commands.has_permissions(manage_channels=True)
async def prefix_setmode(ctx, mode: str = "quiet"):
    if mode not in ("quiet", "detailed"):
        await ctx.send(embed=discord.Embed(description="Mode must be `quiet` or `detailed`.", color=emb.C_RED))
        return
    gid = str(ctx.guild.id)
    cfg = state.get_guild_config(gid)
    cfg["mode"] = mode
    state.set_guild_config(gid, cfg)
    icon = "🔇" if mode == "quiet" else "📢"
    await ctx.send(embed=discord.Embed(description=f"{icon} Mode set to **{mode}**.", color=emb.C_GREEN))


# ── /settimezone  !settimezone ────────────────────────────────────────────────

@tree.command(name="settimezone", description="Set server timezone for daily summary timing")
@app_commands.describe(timezone_name="IANA timezone e.g. America/New_York, Europe/London, Asia/Tokyo")
@app_commands.default_permissions(manage_channels=True)
async def slash_settimezone(interaction: discord.Interaction, timezone_name: str):
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, KeyError):
        await interaction.response.send_message(embed=discord.Embed(
            description=(
                f"❌ `{timezone_name}` is not a valid timezone.\n"
                "Examples: `America/New_York` · `Europe/London` · `Asia/Tokyo` · `UTC`"
            ),
            color=emb.C_RED,
        ), ephemeral=True)
        return
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    cfg["timezone"] = timezone_name
    state.set_guild_config(gid, cfg)
    now_local = datetime.now(tz).strftime("%H:%M")
    await interaction.response.send_message(embed=discord.Embed(
        description=f"🕐 Timezone set to **{timezone_name}**  (current local time: {now_local})\nThe daily summary will send at 23:55 in this timezone.",
        color=emb.C_GREEN,
    ))


@bot.command(name="settimezone", help="Set server timezone. Usage: !settimezone America/New_York")
@commands.has_permissions(manage_channels=True)
async def prefix_settimezone(ctx, *, timezone_name: str = "UTC"):
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, KeyError):
        await ctx.send(embed=discord.Embed(
            description=f"❌ `{timezone_name}` is not a valid IANA timezone.", color=emb.C_RED
        ))
        return
    gid = str(ctx.guild.id)
    cfg = state.get_guild_config(gid)
    cfg["timezone"] = timezone_name
    state.set_guild_config(gid, cfg)
    await ctx.send(embed=discord.Embed(
        description=f"🕐 Timezone set to **{timezone_name}**.", color=emb.C_GREEN
    ))


# ── /timezone  !timezone ──────────────────────────────────────────────────────

@tree.command(name="timezone", description="Show current timezone setting for this server")
async def slash_timezone(interaction: discord.Interaction):
    cfg = state.get_guild_config(str(interaction.guild_id))
    tz_str = cfg.get("timezone", "UTC")
    tz = _tz_for(interaction.guild_id)
    now_local = datetime.now(tz).strftime("%H:%M")
    await interaction.response.send_message(embed=discord.Embed(
        description=f"🕐 Current timezone: **{tz_str}**  (local time: {now_local})",
        color=emb.C_BLUE,
    ))


# ── /status  !status ──────────────────────────────────────────────────────────

@tree.command(name="status", description="Show bot configuration for this server")
async def slash_status(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_status_embed(interaction.guild))


@bot.command(name="status", help="Show bot configuration")
async def prefix_status(ctx):
    await ctx.send(embed=_status_embed(ctx.guild))


def _status_embed(guild: discord.Guild | None) -> discord.Embed:
    em = discord.Embed(title="⚙️  Bot Status", color=emb.C_BLUE)
    if not guild:
        em.description = "Run this command in a server."
        return em
    gid = str(guild.id)
    cfg = state.get_guild_config(gid)
    cid     = cfg.get("channel_id")
    ch      = guild.get_channel(int(cid)) if cid else None
    mode    = cfg.get("mode", "quiet")
    tz_str  = cfg.get("timezone", "UTC")

    # Monitoring status
    mon_running = monitor_loop.is_running()
    if not cid:
        mon_status = "⚠️ Disabled — run `/setchannel` to enable"
    elif mon_running:
        mon_status = "✅ Active"
    else:
        mon_status = "❌ Loop stopped"

    # Count tracked matches (matches with any snapshot or reminder state)
    all_snaps   = len(state._state.get("snapshots", {}))
    all_tracked = len(state._state.get("reminders_sent", {}))
    tracked_str = f"{all_tracked} matches tracked  ·  {all_snaps} with live snapshots"

    em.add_field(
        name="Notification channel",
        value=ch.mention if ch else "Not set — run `/setchannel`",
        inline=False,
    )
    em.add_field(name="Mode",       value=f"`{mode}`",   inline=True)
    em.add_field(name="Timezone",   value=f"`{tz_str}`", inline=True)
    em.add_field(name="Monitoring", value=mon_status,    inline=False)
    em.add_field(name="Match tracking", value=tracked_str, inline=False)
    em.set_footer(text="football-data.org • Kickoff Bot")
    return em


# ── /help  !help ──────────────────────────────────────────────────────────────

@tree.command(name="help", description="Show all available commands")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(embed=emb.embed_help(), ephemeral=True)


@bot.command(name="help", help="Show all available commands")
async def prefix_help(ctx):
    await ctx.send(embed=emb.embed_help())


# ── /testembed  (admin only) ──────────────────────────────────────────────────

_TEST_TYPES = (
    "reminder60", "reminder15", "kickoff", "goal", "redcard",
    "halftime", "secondhalf", "extratime", "pso", "fulltime", "poll",
)

@tree.command(name="testembed", description="[Admin] Preview a notification embed with test data")
@app_commands.describe(embed_type="Type of embed to preview")
@app_commands.choices(embed_type=[app_commands.Choice(name=t, value=t) for t in _TEST_TYPES])
@app_commands.default_permissions(administrator=True)
async def slash_testembed(interaction: discord.Interaction, embed_type: str):
    em = emb.embed_test(embed_type)
    await interaction.response.send_message(embed=em, ephemeral=True)


@bot.command(name="testembed", help="[Admin] Test embed. Usage: !testembed goal")
@commands.has_permissions(administrator=True)
async def prefix_testembed(ctx, embed_type: str = "goal"):
    await ctx.send(embed=emb.embed_test(embed_type))


# ══════════════════════════════════════════════════════════════════════════════
#  ERROR HANDLING
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=discord.Embed(description="🔒 You need the **Manage Channels** permission.", color=emb.C_RED))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=discord.Embed(description=f"⚠️ Missing argument — see `!help {ctx.command.name}`.", color=emb.C_ORANGE))
    else:
        log.error("[ERROR] Command %s: %s", ctx.command, error, exc_info=True)
        await ctx.send(embed=discord.Embed(description="⚠️ Something went wrong.", color=emb.C_RED))


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = "⚠️ Something went wrong."
    if isinstance(error, app_commands.MissingPermissions):
        msg = "🔒 You need the required permissions to run this command."
    log.error("[ERROR] Slash command error: %s", error, exc_info=True)
    try:
        await interaction.followup.send(
            embed=discord.Embed(description=msg, color=emb.C_RED), ephemeral=True
        )
    except discord.HTTPException:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
