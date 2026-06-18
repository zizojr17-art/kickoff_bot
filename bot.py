"""
bot.py — FIFA World Cup 2026 companion bot  (v2.1)
World Cup 2026-only: hardcoded to WC, no competition switching.

v2.1 improvements
─────────────────
• Monitor loop reduced from 2 min → 60 s for faster notifications
• All broadcast functions parallelised with asyncio.gather — every
  guild receives alerts simultaneously instead of sequentially
• Reminder window widened (±2 min) so 60/15-min reminders are never missed
• MOTM vote bug fixed — vote now fires even when match status stays
  IN_PLAY throughout (was only checked on status transitions)
• Autocomplete added to /predict, /match, /matchfacts — pick a match
  from a live dropdown showing flag + teams + kick-off time in your TZ
• Team-flag autocomplete for /team, /followteam, /unfollowteam
• Team flags now shown in the score prediction UI (buttons & preview)
• _annotate_match now injects kickoffTs (Unix timestamp) so embeds.py
  can use <t:{kickoffTs}:F> / <t:{kickoffTs}:R> for TZ-correct times
• WC match order auto-refreshed every 60 min (no more blank match #s)
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any

if os.path.exists(".env"):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

import discord
from discord import app_commands
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import state
import embeds as emb
from football_api import (
    WC_TEAMS,
    WC_GROUPS,
    close_session,
    get_competition_matches,
    get_current_score,
    get_minute,
    get_live_matches,
    get_match_detail,
    get_match_motm,
    get_next_match,
    get_score,
    get_standings,
    get_team,
    get_team_matches,
    get_todays_matches,
    goal_key,
    card_key,
    has_confirmed_lineups,
    is_knockout,
    load_wc_match_order,
    get_wc_match_order,
    parse_dt,
    search_team,
    search_youtube_highlights,
    team_display,
    team_flag,
    get_scorers,
    get_player_wc_stats,
    get_match_stats,
    get_tournament_stats,
    get_api_diagnostics,
    STAGE_NAMES,
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

# ── Bot ────────────────────────────────────────────────────────────────────────

_ENV_STATUS = {
    "DISCORD_BOT_TOKEN": bool(os.environ.get("DISCORD_BOT_TOKEN")),
    "DISCORD_TOKEN": bool(os.environ.get("DISCORD_TOKEN")),
    "FOOTBALL_DATA_API_KEY": bool(os.environ.get("FOOTBALL_DATA_API_KEY")),
    "BOT_STATE_FILE": bool(os.environ.get("BOT_STATE_FILE")),
}
log.info("[ENV] Present vars: %s", ", ".join(k for k, v in _ENV_STATUS.items() if v) or "none")
log.info("[ENV] Missing vars: %s", ", ".join(k for k, v in _ENV_STATUS.items() if not v) or "none")

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    log.critical("[ENV] Missing Discord token. Set DISCORD_BOT_TOKEN (preferred) or DISCORD_TOKEN.")
    sys.exit(1)

intents = discord.Intents.default()
bot  = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

state.load()

# ── Auto-help registry ────────────────────────────────────────────────────────

COMMAND_REGISTRY: dict[str, list[dict]] = {
    "Match Commands": [],
    "Info":           [],
    "Predictions":    [],
    "Following":      [],
    "Settings":       [],
    "Admin":          [],
}


def _register(category: str, name: str, description: str) -> None:
    COMMAND_REGISTRY.setdefault(category, []).append({"name": name, "description": description})


# ── Constants ─────────────────────────────────────────────────────────────────

_MODE_RANK: dict[str, int] = {"quiet": 0, "standard": 1, "detailed": 2}

_USER_SETTING_LABELS: dict[str, str] = {
    "goals":              "⚽ Goals",
    "red_cards":          "🟥 Red Cards",
    "kickoff":            "🔔 Kick-off",
    "halftime":           "⏱️ Half Time",
    "fulltime":           "🏁 Full Time",
    "lineups":            "📋 Lineups",
    "motm_vote":          "🌟 MOTM Voting",
    "motm_results":       "🏆 MOTM Results",
    "predictions":        "🎯 Prediction Polls",
    "prediction_results": "📊 Prediction Results",
    "recaps":             "📺 Recaps",
    "daily_summary":      "📅 Daily Summary",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  MATCH NUMBERING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _match_num(match_id: int | str) -> int | None:
    """Return the official WC match number (1-indexed), or None if unknown."""
    order = get_wc_match_order()
    if not order:
        return None
    try:
        return order.index(int(match_id)) + 1
    except (ValueError, TypeError):
        return None


def _match_num_str(match_id: int | str) -> str:
    """Return formatted WC match number like '#42' or '' if unknown."""
    n = _match_num(match_id)
    return f"#{n}" if n is not None else ""


def _annotate_match(match: dict) -> dict:
    """Return a copy of the match dict with extra display keys injected.

    Added keys
    ----------
    matchNumber  : int | None  — official WC match number (1-indexed)
    matchDay     : int | None  — matchday from the API (1, 2, 3 …)
    kickoffTs    : int | None  — UTC Unix timestamp of kick-off.
                                 Use ``<t:{kickoffTs}:F>`` in embeds for Discord's
                                 client-side timezone rendering (always correct for
                                 every user), and ``<t:{kickoffTs}:R>`` for a
                                 live countdown.  Fixes the "wrong timezone countdown"
                                 issue without any server-side timezone arithmetic.
    """
    m = dict(match)
    m["matchNumber"] = _match_num(match.get("id", 0))
    m["matchDay"]    = match.get("matchday")   # integer from API, e.g. 1 / 2 / 3
    dt = parse_dt(match.get("utcDate", ""))
    m["kickoffTs"]   = int(dt.timestamp()) if dt else None
    # Competition day: Day 1 = 11 Jun 2026 (WC 2026 opening match)
    _WC_START = datetime(2026, 6, 11, tzinfo=timezone.utc)
    if dt:
        comp_day = (dt.replace(hour=0, minute=0, second=0, microsecond=0) -
                    _WC_START.replace(hour=0, minute=0, second=0, microsecond=0)).days + 1
        m["competitionDay"] = comp_day if comp_day >= 1 else None
    else:
        m["competitionDay"] = None
    return m


def _annotate_matches(matches: list[dict]) -> list[dict]:
    """Annotate a list of matches with official WC match numbers."""
    return [_annotate_match(m) for m in matches]


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _tz_for(gid: Any) -> ZoneInfo:
    cfg    = state.get_guild_config(str(gid))
    tz_str = cfg.get("timezone", "UTC")
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


def _user_tz_for(uid: str) -> ZoneInfo:
    """Return the user's personal timezone, defaulting to UTC."""
    tz_str = state.get_user_following(uid).get("timezone", "UTC")
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


def _user_tz_str(uid: str) -> str:
    """Return the user's timezone name string, or 'UTC'."""
    return state.get_user_following(uid).get("timezone", "UTC")


def _fmt_kickoff(utc_iso: str, tz: ZoneInfo) -> str:
    """Format a UTC ISO datetime into local time for display."""
    dt = parse_dt(utc_iso)
    if not dt:
        return "TBD"
    local = dt.astimezone(tz)
    return local.strftime("%d %b  %H:%M %Z")


# Common timezones available throughout the bot for user selection
async def autocomplete_timezone(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Fuzzy-search autocomplete for timezone parameters."""
    current_lower = current.lower()
    matches: list[app_commands.Choice[str]] = []
    for opt in _USER_TZ_OPTIONS:
        if current_lower in opt.label.lower() or current_lower in opt.value.lower():
            matches.append(app_commands.Choice(name=opt.label, value=opt.value))
        if len(matches) >= 25:
            break
    if not matches:
        matches = [app_commands.Choice(name=opt.label, value=opt.value) for opt in _USER_TZ_OPTIONS[:25]]
    return matches


_USER_TZ_OPTIONS: list[discord.SelectOption] = [
    discord.SelectOption(label="UTC (default)",                     value="UTC",                              emoji="🌐"),
    discord.SelectOption(label="US Eastern  (UTC−5/−4)",            value="America/New_York",                  emoji="🇺🇸"),
    discord.SelectOption(label="US Central  (UTC−6/−5)",            value="America/Chicago",                   emoji="🇺🇸"),
    discord.SelectOption(label="US Mountain (UTC−7/−6)",            value="America/Denver",                    emoji="🇺🇸"),
    discord.SelectOption(label="US Pacific  (UTC−8/−7)",            value="America/Los_Angeles",               emoji="🇺🇸"),
    discord.SelectOption(label="Canada / Toronto",                  value="America/Toronto",                   emoji="🇨🇦"),
    discord.SelectOption(label="Canada / Vancouver",                value="America/Vancouver",                 emoji="🇨🇦"),
    discord.SelectOption(label="UK / Ireland  (UTC+0/+1)",          value="Europe/London",                     emoji="🇬🇧"),
    discord.SelectOption(label="Central Europe  (UTC+1/+2)",        value="Europe/Berlin",                     emoji="🇩🇪"),
    discord.SelectOption(label="Eastern Europe  (UTC+2/+3)",        value="Europe/Bucharest",                  emoji="🇷🇴"),
    discord.SelectOption(label="Brazil – Brasília  (UTC−3)",        value="America/Sao_Paulo",                 emoji="🇧🇷"),
    discord.SelectOption(label="Argentina  (UTC−3)",                value="America/Argentina/Buenos_Aires",    emoji="🇦🇷"),
    discord.SelectOption(label="Mexico City  (UTC−6/−5)",           value="America/Mexico_City",               emoji="🇲🇽"),
    discord.SelectOption(label="Colombia / Ecuador  (UTC−5)",       value="America/Bogota",                    emoji="🇨🇴"),
    discord.SelectOption(label="Saudi Arabia / UAE+1  (UTC+3)",     value="Asia/Riyadh",                       emoji="🇸🇦"),
    discord.SelectOption(label="UAE / Oman  (UTC+4)",               value="Asia/Dubai",                        emoji="🇦🇪"),
    discord.SelectOption(label="India  (UTC+5:30)",                 value="Asia/Kolkata",                      emoji="🇮🇳"),
    discord.SelectOption(label="Bangladesh / Myanmar+30  (UTC+6)",  value="Asia/Dhaka",                        emoji="🇧🇩"),
    discord.SelectOption(label="Indonesia / Thailand  (UTC+7)",     value="Asia/Bangkok",                      emoji="🇹🇭"),
    discord.SelectOption(label="China / Singapore  (UTC+8)",        value="Asia/Shanghai",                     emoji="🇨🇳"),
    discord.SelectOption(label="Japan / South Korea  (UTC+9)",      value="Asia/Tokyo",                        emoji="🇯🇵"),
    discord.SelectOption(label="Australia – Sydney  (UTC+10/+11)",  value="Australia/Sydney",                  emoji="🇦🇺"),
    discord.SelectOption(label="Australia – Perth  (UTC+8)",        value="Australia/Perth",                   emoji="🇦🇺"),
    discord.SelectOption(label="New Zealand  (UTC+12/+13)",         value="Pacific/Auckland",                  emoji="🇳🇿"),
    discord.SelectOption(label="Morocco / Senegal  (UTC+0/+1)",     value="Africa/Casablanca",                 emoji="🇲🇦"),
]


def _mode_at_least(gid: str, required: str) -> bool:
    # req #13: mode-gating removed — all guilds receive all notifications
    return True


def _same_result(ph: int, pa: int, ah: int, aa: int) -> bool:
    if ph > pa and ah > aa:
        return True
    if ph < pa and ah < aa:
        return True
    if ph == pa and ah == aa:
        return True
    return False


async def _safe_defer(interaction: discord.Interaction) -> None:
    try:
        await interaction.response.defer()
    except discord.HTTPException:
        pass


async def _send_embeds(interaction: discord.Interaction, embeds: list[discord.Embed]) -> None:
    """Send multiple embeds as follow-up messages, splitting at 10 per batch."""
    if not embeds:
        await interaction.followup.send(
            embed=discord.Embed(description="No data found.", color=emb.C_GREY)
        )
        return
    for i in range(0, len(embeds), 10):
        await interaction.followup.send(embeds=embeds[i:i + 10])


def _error_embed(message: str, suggestion: str = "") -> discord.Embed:
    em = discord.Embed(description=f"❌ {message}", color=emb.C_RED)
    if suggestion:
        em.add_field(name="💡 Try this", value=suggestion, inline=False)
    return em


def _success_embed(message: str) -> discord.Embed:
    return discord.Embed(description=f"✅ {message}", color=emb.C_GREEN)


# ═══════════════════════════════════════════════════════════════════════════════
#  BROADCAST HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _resolve_channel(
    cid: str,
) -> discord.TextChannel | discord.Thread | None:
    """Return the channel/thread from cache, falling back to a Discord API fetch.

    Using only ``bot.get_channel`` is unreliable — the channel may not be in
    Discord.py's internal cache after a restart or on a shard miss, which
    causes every notification to be silently dropped.  The fetch fallback
    ensures delivery even when the cache is cold.
    """
    ch = bot.get_channel(int(cid))
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
        return ch
    try:
        ch = await bot.fetch_channel(int(cid))
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            return ch
    except discord.HTTPException as e:
        log.warning("[BROADCAST] Could not resolve channel %s: %s", cid, e)
    return None


async def _send_to_channel(gid: str, cfg: dict, key: str, **kwargs) -> None:
    cid = cfg.get(key)
    if not cid:
        return
    ch = await _resolve_channel(cid)
    if ch is None:
        return
    try:
        await ch.send(**kwargs)
    except discord.Forbidden:
        log.warning("[BROADCAST] Missing permissions in channel %s (guild %s)", cid, gid)
    except discord.HTTPException as e:
        log.error("[BROADCAST] Failed to send to %s: %s", cid, e)


async def _send_to_channel_return(gid: str, cfg: dict, key: str, **kwargs) -> discord.Message | None:
    cid = cfg.get(key)
    if not cid:
        return None
    ch = await _resolve_channel(cid)
    if ch is None:
        return None
    try:
        return await ch.send(**kwargs)
    except discord.HTTPException as e:
        log.error("[BROADCAST] Failed: %s", e)
    return None


async def _send_with_retry(
    ch: discord.TextChannel | discord.Thread,
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    **kwargs,
) -> discord.Message | None:
    """req #7: Send a message with exponential-backoff retry on transient HTTP errors."""
    for attempt in range(1, max_attempts + 1):
        try:
            return await ch.send(**kwargs)
        except discord.HTTPException as e:
            if attempt == max_attempts or e.status not in (429, 500, 502, 503, 504):
                log.error("[SEND_RETRY] Gave up after %d attempt(s): %s", attempt, e)
                return None
            wait = base_delay * (2 ** (attempt - 1))
            log.warning("[SEND_RETRY] Attempt %d/%d failed (%s), retrying in %.1fs",
                        attempt, max_attempts, e.status, wait)
            await asyncio.sleep(wait)
    return None


async def broadcast(embed: discord.Embed, min_mode: str = "quiet") -> None:
    # req #1/#13: min_mode guard removed — all guilds receive broadcasts
    coros = [
        _send_to_channel(gid, cfg, "channel_id", embed=embed)
        for gid, cfg in state.all_guild_configs().items()
    ]
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def broadcast_predictions(embed: discord.Embed, view: discord.ui.View | None = None) -> None:
    # req #13: mode gate removed — all guilds receive prediction broadcasts
    coros = []
    for gid, cfg in state.all_guild_configs().items():
        kwargs: dict[str, Any] = {"embed": embed}
        if view:
            kwargs["view"] = view
        coros.append(_send_to_channel(gid, cfg, "predictions_channel_id", **kwargs))
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def broadcast_results(embed: discord.Embed) -> None:
    """Post prediction/MOTM results to the unified predictions channel."""
    # req #13: mode gate removed — all guilds receive results
    coros = [
        _send_to_channel(gid, cfg, "predictions_channel_id", embed=embed)
        for gid, cfg in state.all_guild_configs().items()
    ]
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def broadcast_summary(embed: discord.Embed) -> None:
    """Post recap to summary channel for standard+ guilds; fall back to predictions channel."""
    coros = []
    for gid, cfg in state.all_guild_configs().items():
        if _mode_at_least(gid, "standard"):
            key = "summary_channel_id" if cfg.get("summary_channel_id") else "predictions_channel_id"
            coros.append(_send_to_channel(gid, cfg, key, embed=embed))
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def _update_leaderboard_in_predictions(gid: str, cfg: dict) -> None:
    lb = state.get_leaderboard(gid)
    if not lb:
        return
    guild = bot.get_guild(int(gid))
    if not guild:
        return

    lb_embed = emb.embed_leaderboard(guild, lb)
    # Post to unified predictions channel; fall back to legacy results_channel_id
    cid = cfg.get("predictions_channel_id") or cfg.get("results_channel_id")
    if not cid:
        return
    ch = bot.get_channel(int(cid))
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return

    old_id = state.get_pinned_leaderboard(gid)
    if old_id:
        try:
            old = await ch.fetch_message(int(old_id))
            await old.unpin()
        except discord.HTTPException:
            pass

    try:
        msg = await ch.send(embed=lb_embed)
        await msg.pin()
        state.save_pinned_leaderboard(gid, msg.id)
    except discord.HTTPException as e:
        log.error("[LEADERBOARD] Failed to post: %s", e)


# ── Match thread helpers ──────────────────────────────────────────────────────

def _match_thread_name(match: dict) -> str:
    home = team_display(match.get("homeTeam", {}))
    away = team_display(match.get("awayTeam", {}))
    hf   = team_flag(match.get("homeTeam", {}))
    af   = team_flag(match.get("awayTeam", {}))
    return f"{hf} {home} vs {away} {af}"[:100]


_THREAD_ARCHIVE_CODES = {50083, 160005}  # archived / locked

# In-process cache of live Thread objects (key: "{gid}:{mid}").
# Avoids a Discord REST round-trip on every event and prevents the
# guild.fetch_channel() reliability issues seen with standalone threads.
_thread_obj_cache: dict[str, discord.Thread] = {}


async def _unarchive_thread(thread: discord.Thread) -> bool:
    """Attempt to unarchive a thread. Returns True on success."""
    try:
        await thread.edit(archived=False, reason="Reactivating for live match event")
        return True
    except discord.HTTPException as e:
        log.warning("[THREAD] Could not unarchive %s: %s", thread.id, e)
        return False


async def _fetch_thread(thread_id: str) -> discord.Thread | None:
    """Fetch a thread by ID using the most reliable path (bot.fetch_channel).

    guild.fetch_channel() can fail to properly resolve standalone threads on
    some discord.py builds.  bot.fetch_channel() calls GET /channels/{id}
    directly and always returns the correct type.
    """
    # 1. Bot's internal cache (instant, no API call)
    t = bot.get_channel(int(thread_id))
    if isinstance(t, discord.Thread):
        return t
    # 2. Full REST fetch — most reliable across all discord.py 2.x builds
    try:
        t = await bot.fetch_channel(int(thread_id))
        if isinstance(t, discord.Thread):
            return t
        log.warning("[THREAD] fetch_channel(%s) returned %s, not Thread", thread_id, type(t))
        return None
    except discord.HTTPException as e:
        log.warning("[THREAD] fetch_channel(%s) failed: %s", thread_id, e)
        return None


async def _get_or_create_match_thread(
    match: dict, gid: str, cfg: dict
) -> discord.Thread | None:
    mid       = str(match["id"])
    cid       = cfg.get("channel_id")
    cache_key = f"{gid}:{mid}"

    if not cid:
        return None

    # ── 1. In-memory object cache (fastest; valid for the lifetime of this process) ──
    t = _thread_obj_cache.get(cache_key)
    if isinstance(t, discord.Thread):
        if not t.archived:
            return t
        # Archived — try to reopen before falling through to fetch
        if await _unarchive_thread(t):
            return t
        del _thread_obj_cache[cache_key]

    # ── 2. Resolve the parent TextChannel ──────────────────────────────────────────
    ch = bot.get_channel(int(cid))
    if not isinstance(ch, discord.TextChannel):
        try:
            ch = await bot.fetch_channel(int(cid))
        except discord.HTTPException as e:
            log.warning("[THREAD] Could not resolve live channel %s: %s", cid, e)
            return None
    if not isinstance(ch, discord.TextChannel):
        return None

    # ── 3. Re-hydrate thread from stored ID ────────────────────────────────────────
    thread_id = state.get_match_thread(mid, gid)
    if thread_id:
        t = await _fetch_thread(thread_id)
        if isinstance(t, discord.Thread):
            if t.archived:
                await _unarchive_thread(t)
            _thread_obj_cache[cache_key] = t
            return t
        log.warning("[THREAD] Stored thread %s unreachable — creating a new one", thread_id)

    # ── 4. Create a new standalone thread — with retry (req #17) ──────────────────
    thread_name = _match_thread_name(match)
    _last_thread_err: discord.HTTPException | None = None
    for _attempt in range(1, 4):
        try:
            t = await ch.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=10080,
            )
            state.save_match_thread(mid, gid, t.id)
            _thread_obj_cache[cache_key] = t
            log.info("[THREAD] Created '%s' for match %s in guild %s (attempt %d)",
                     thread_name, mid, gid, _attempt)
            return t
        except discord.HTTPException as e:
            _last_thread_err = e
            if _attempt < 3 and e.status in (429, 500, 502, 503, 504):
                await asyncio.sleep(2.0 * _attempt)
                continue
            break
    if _last_thread_err is not None:
        log.error("[THREAD] Failed to create thread for match %s guild %s: %s", mid, gid, _last_thread_err)
    return None


async def _send_to_match_thread(
    match: dict, gid: str, cfg: dict, **kwargs
) -> discord.Message | None:
    thread = await _get_or_create_match_thread(match, gid, cfg)
    if not thread:
        return None
    try:
        return await thread.send(**kwargs)
    except discord.Forbidden as e:
        log.error("[THREAD] No permission to send in thread %s guild %s: %s", thread.id, gid, e)
        return None
    except discord.HTTPException as e:
        if e.code in _THREAD_ARCHIVE_CODES:
            log.warning("[THREAD] Thread %s archived during send (code %s) — unarchiving and retrying", thread.id, e.code)
            if await _unarchive_thread(thread):
                try:
                    return await thread.send(**kwargs)
                except discord.HTTPException as e2:
                    log.error("[THREAD] Retry after unarchive failed for thread %s: %s", thread.id, e2)
            # Evict stale object so the next call re-fetches
            mid = str(match["id"])
            for k in list(_thread_obj_cache):
                if k.endswith(f":{mid}"):
                    del _thread_obj_cache[k]
        else:
            log.error("[THREAD] Send failed in thread %s (code %s): %s", thread.id, e.code, e)
        return None


async def broadcast_to_threads(match: dict, min_mode: str = "quiet", **kwargs) -> bool:
    """Send to the per-match thread in every configured guild."""
    # req #13: min_mode filter removed — all guilds receive thread broadcasts
    coros = [
        _send_to_match_thread(match, gid, cfg, **kwargs)
        for gid, cfg in state.all_guild_configs().items()
    ]
    if not coros:
        return False
    results = await asyncio.gather(*coros, return_exceptions=True)
    sent_any = False
    for r in results:
        if isinstance(r, Exception):
            log.error("[THREAD] broadcast_to_threads unhandled error: %s", r)
        elif r is not None:
            sent_any = True
    return sent_any


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTOCOMPLETE PROVIDERS
# ═══════════════════════════════════════════════════════════════════════════════

async def autocomplete_team(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current_lower = current.lower()
    matches = [t for t in WC_TEAMS if current_lower in t.lower()][:25]
    return [
        app_commands.Choice(name=f"{team_flag({'name': t})} {t}", value=t)
        for t in matches
    ]


async def autocomplete_match_id(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    """Autocomplete upcoming match IDs with human-readable names."""
    try:
        today = datetime.now(timezone.utc)
        uid   = str(interaction.user.id)
        tz    = _user_tz_for(uid)
        matches = await get_competition_matches(
            today.strftime("%Y-%m-%d"),
            (today + timedelta(days=14)).strftime("%Y-%m-%d"),
        )
    except Exception:
        return []

    choices: list[app_commands.Choice[int]] = []
    current_lower = current.lower()
    for m in matches:
        mid  = m.get("id", 0)
        home = team_display(m.get("homeTeam", {}))
        away = team_display(m.get("awayTeam", {}))
        hf   = team_flag(m.get("homeTeam", {}))
        af   = team_flag(m.get("awayTeam", {}))
        num  = _match_num(mid)
        dt   = parse_dt(m.get("utcDate", ""))
        date_str = _fmt_kickoff(m.get("utcDate", ""), tz) if dt else "TBD"
        num_str  = f"#{num} · " if num else ""
        label    = f"{num_str}{hf} {home} vs {af} {away} — {date_str}"[:100]
        if not current_lower or current_lower in label.lower() or current_lower in str(mid):
            choices.append(app_commands.Choice(name=label, value=mid))
        if len(choices) >= 25:
            break
    return choices


async def autocomplete_group(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current_upper = current.upper()
    matches = [g for g in WC_GROUPS if g.startswith(current_upper) or not current][:25]
    return [app_commands.Choice(name=f"Group {g}", value=g) for g in matches]


async def autocomplete_followed_team(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    uid      = str(interaction.user.id)
    following = state.get_user_following(uid)
    teams    = following.get("teams", [])
    current_lower = current.lower()
    matches  = [t for t in teams if current_lower in t.lower()][:25]
    return [
        app_commands.Choice(name=f"{team_flag({'name': t})} {t}", value=t)
        for t in matches
    ]


# ═══════════════════════════════════════════════════════════════════════════════
#  DISCORD UI VIEWS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Score prediction ──────────────────────────────────────────────────────────

class ScoreInputView(discord.ui.View):
    """Ephemeral per-user score prediction input with +/- buttons."""

    def __init__(self, match_id: str, home_team: str, away_team: str, knockout: bool = False):
        super().__init__(timeout=5400)
        self.match_id   = match_id
        self.home_name  = home_team[:14]
        self.away_name  = away_team[:14]
        self.home_flag  = team_flag({"name": home_team})
        self.away_flag  = team_flag({"name": away_team})
        self.knockout   = knockout
        self.home_score = 0
        self.away_score = 0
        self.changed    = False
        self._sync()

    def _is_draw(self) -> bool:
        return self.knockout and self.home_score == self.away_score

    def _sync(self) -> None:
        draw = self._is_draw()
        self.home_label_btn.label    = f"{self.home_flag} {self.home_name}  {self.home_score}"
        self.away_label_btn.label    = f"{self.away_flag} {self.away_name}  {self.away_score}"
        self.home_minus_btn.disabled = self.home_score == 0
        self.home_plus_btn.disabled  = self.home_score >= 9
        self.away_minus_btn.disabled = self.away_score == 0
        self.away_plus_btn.disabled  = self.away_score >= 9
        self.lock_btn.disabled       = not self.changed or draw
        self.lock_btn.label          = "⚠️ No draws in knockout" if draw else "🔒 Lock Prediction"

    def _content(self) -> str:
        ko = "\n⚠️ *Draws not allowed in knockout — one team must win.*" \
            if self._is_draw() and self.changed else ""
        return (
            f"**Your prediction:**\n"
            f"{self.home_flag} **{self.home_name}**  {self.home_score} – {self.away_score}  **{self.away_name}** {self.away_flag}"
            f"{ko}"
        )

    @discord.ui.button(label="−", style=discord.ButtonStyle.secondary, row=0)
    async def home_minus_btn(self, i: discord.Interaction, b: discord.ui.Button):
        self.home_score = max(0, self.home_score - 1)
        self.changed = True
        self._sync()
        await i.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="Home 0", style=discord.ButtonStyle.primary, row=0, disabled=True)
    async def home_label_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer()

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=0)
    async def home_plus_btn(self, i: discord.Interaction, b: discord.ui.Button):
        self.home_score = min(9, self.home_score + 1)
        self.changed = True
        self._sync()
        await i.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="−", style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def away_minus_btn(self, i: discord.Interaction, b: discord.ui.Button):
        self.away_score = max(0, self.away_score - 1)
        self.changed = True
        self._sync()
        await i.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="Away 0", style=discord.ButtonStyle.primary, row=1, disabled=True)
    async def away_label_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer()

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=1)
    async def away_plus_btn(self, i: discord.Interaction, b: discord.ui.Button):
        self.away_score = min(9, self.away_score + 1)
        self.changed = True
        self._sync()
        await i.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="🔒 Lock Prediction", style=discord.ButtonStyle.success, row=2, disabled=True)
    async def lock_btn(self, i: discord.Interaction, b: discord.ui.Button):
        if self._is_draw():
            await i.response.send_message(
                "❌ Draws are not allowed in knockout matches.", ephemeral=True
            )
            return
        state.save_score_prediction(str(i.user.id), self.match_id, self.home_score, self.away_score)
        for child in self.children:
            child.disabled = True
        await i.response.edit_message(
            content=(
                f"🎯 **Locked in: {self.home_score}–{self.away_score}** — "
                f"may the odds be ever in your favour!"
            ),
            view=self,
        )
        self.stop()


class PredictionView(discord.ui.View):
    """Attached to prediction poll. Opens ephemeral ScoreInputView."""

    def __init__(self, match_id: str, home_team: str, away_team: str, knockout: bool = False):
        super().__init__(timeout=5400)
        self.match_id  = match_id
        self.home_team = home_team
        self.away_team = away_team
        self.knockout  = knockout

    @discord.ui.button(label="🎯 Predict Score", style=discord.ButtonStyle.primary)
    async def predict_btn(self, i: discord.Interaction, b: discord.ui.Button):
        # Hard lockout — predictions window closes at kick-off
        if state.is_prediction_locked(self.match_id):
            await i.response.send_message(
                "🔒 **Predictions are locked** — this match has already kicked off!\n"
                "Check the predictions channel after full time to see how you did.",
                ephemeral=True,
            )
            return
        view     = ScoreInputView(self.match_id, self.home_team, self.away_team, self.knockout)
        existing = state.get_score_predictions(self.match_id).get(str(i.user.id))
        if existing:
            view.home_score = existing["home"]
            view.away_score = existing["away"]
            view.changed    = True
            view._sync()
            content = (
                f"**Your current prediction:** {existing['home']}–{existing['away']}\n"
                "You can update it until kick-off."
            )
        else:
            content = view._content()
        await i.response.send_message(content=content, view=view, ephemeral=True)


# ── MOTM voting ───────────────────────────────────────────────────────────────

class MotmVoteView(discord.ui.View):
    def __init__(self, match_id: str, nominees: list[str]):
        super().__init__(timeout=7200)
        self.match_id = match_id
        options = [discord.SelectOption(label=n[:100], value=n[:100]) for n in nominees[:25]]
        select  = discord.ui.Select(
            placeholder="🌟 Vote for Man of the Match…",
            min_values=1, max_values=1,
            options=options,
        )
        select.callback = self._on_vote
        self.add_item(select)

    async def _on_vote(self, i: discord.Interaction) -> None:
        selected = i.data["values"][0]
        gid      = str(i.guild_id)
        votes    = state.get_motm_votes(self.match_id, gid)
        votes[str(i.user.id)] = selected
        state.save_motm_votes(self.match_id, gid, votes)
        await i.response.send_message(
            f"✅ MOTM vote recorded: **{selected}**\nYou can change your vote until full time.",
            ephemeral=True,
        )


# ── Notification mode picker ──────────────────────────────────────────────────

class ModePicker(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Personal timezone dropdown (persistent — needs custom_id)
        tz_select = discord.ui.Select(
            placeholder="🌍 Set your personal timezone…",
            min_values=1, max_values=1,
            options=list(_USER_TZ_OPTIONS),
            custom_id="mp_timezone",
            row=1,
        )
        tz_select.callback = self._on_timezone
        self.add_item(tz_select)

    async def _on_timezone(self, i: discord.Interaction) -> None:
        tz_name  = i.data["values"][0]
        uid      = str(i.user.id)
        following = dict(state.get_user_following(uid))
        following["timezone"] = tz_name
        state.set_user_following(uid, following)
        try:
            tz    = ZoneInfo(tz_name)
            local = datetime.now(tz).strftime("%H:%M %Z")
            note  = f" Your local time right now: **{local}**."
        except Exception:
            note  = ""
        await i.response.send_message(
            f"🌍 Timezone set to **{tz_name}**.{note}\n"
            "Match kick-off times in `/today`, `/upcoming`, and `/nextmatch` will use this timezone.",
            ephemeral=True,
        )
        log.info("[SETTINGS] User %s set timezone to %s via commands channel", uid, tz_name)

    @discord.ui.button(label="🔇 Quiet",    style=discord.ButtonStyle.secondary, custom_id="mp_quiet",    row=0)
    async def quiet(self, i: discord.Interaction, b: discord.ui.Button):
        state.set_user_mode(str(i.guild_id), str(i.user.id), "quiet")
        await i.response.send_message(
            "🔇 **Quiet mode** set!\nYou'll receive: Goals · Red Cards · Full Time only.", ephemeral=True
        )

    @discord.ui.button(label="📢 Standard", style=discord.ButtonStyle.primary,   custom_id="mp_standard", row=0)
    async def standard(self, i: discord.Interaction, b: discord.ui.Button):
        state.set_user_mode(str(i.guild_id), str(i.user.id), "standard")
        await i.response.send_message(
            "📢 **Standard mode** set!\nYou'll receive: Goals · Cards · HT · FT · ET · MOTM Polls · Predictions.", ephemeral=True
        )

    @discord.ui.button(label="📋 Detailed", style=discord.ButtonStyle.success,   custom_id="mp_detailed", row=0)
    async def detailed(self, i: discord.Interaction, b: discord.ui.Button):
        state.set_user_mode(str(i.guild_id), str(i.user.id), "detailed")
        await i.response.send_message(
            "📋 **Detailed mode** set!\nYou'll receive everything: Lineups · All events · Recaps · Group tables.", ephemeral=True
        )


# ── User notification settings ────────────────────────────────────────────────

class UserSettingsSelect(discord.ui.Select):
    def __init__(self, uid: str, gid: str):
        self.uid = uid
        self.gid = gid
        prefs    = state.get_user_prefs(uid, gid)
        options  = [
            discord.SelectOption(
                label=label, value=key,
                description="Currently ON" if prefs.get(key, True) else "Currently OFF",
                emoji="✅" if prefs.get(key, True) else "❌",
            )
            for key, label in _USER_SETTING_LABELS.items()
        ]
        super().__init__(
            placeholder="Select notifications to toggle…",
            min_values=1, max_values=len(options),
            options=options, row=0,
        )

    async def callback(self, i: discord.Interaction) -> None:
        prefs = dict(state.get_user_prefs(self.uid, self.gid))
        for key in self.values:
            prefs[key] = not prefs.get(key, True)
        state.set_user_prefs(self.uid, self.gid, prefs)
        self.options = [
            discord.SelectOption(
                label=label, value=key,
                description="Currently ON" if prefs.get(key, True) else "Currently OFF",
                emoji="✅" if prefs.get(key, True) else "❌",
            )
            for key, label in _USER_SETTING_LABELS.items()
        ]
        await i.response.edit_message(
            embed=_build_settings_embed(self.uid, self.gid), view=self.view
        )


class UserTimezoneSelect(discord.ui.Select):
    """Dropdown for selecting the user's personal timezone."""

    def __init__(self, uid: str, gid: str):
        self.uid = uid
        self.gid = gid
        current  = _user_tz_str(uid)
        # Mark the currently-selected timezone
        options  = [
            discord.SelectOption(
                label=opt.label, value=opt.value, emoji=opt.emoji,
                default=(opt.value == current),
            )
            for opt in _USER_TZ_OPTIONS
        ]
        super().__init__(
            placeholder=f"🕐 Your timezone: {current}",
            min_values=1, max_values=1,
            options=options, row=1,
        )

    async def callback(self, i: discord.Interaction) -> None:
        tz_name  = self.values[0]
        following = dict(state.get_user_following(self.uid))
        following["timezone"] = tz_name
        state.set_user_following(self.uid, following)

        # Confirm with current local time so users can verify it's correct
        try:
            tz    = ZoneInfo(tz_name)
            local = datetime.now(tz).strftime("%H:%M")
            note  = f"Your local time right now: **{local}**"
        except Exception:
            note = ""

        await i.response.edit_message(
            embed=_build_settings_embed(self.uid, self.gid), view=self.view,
            content=f"✅ Timezone set to **{tz_name}**. {note}"
        )


class UserSettingsView(discord.ui.View):
    def __init__(self, uid: str, gid: str):
        super().__init__(timeout=300)
        self.uid = uid
        self.gid = gid
        self.add_item(UserSettingsSelect(uid, gid))
        self.add_item(UserTimezoneSelect(uid, gid))

    @discord.ui.button(label="🔄 Reset to Defaults", style=discord.ButtonStyle.danger, row=2)
    async def reset_btn(self, i: discord.Interaction, b: discord.ui.Button):
        state.set_user_prefs(self.uid, self.gid, {})
        new_view = UserSettingsView(self.uid, self.gid)
        await i.response.edit_message(
            embed=_build_settings_embed(self.uid, self.gid), view=new_view
        )


def _build_settings_embed(uid: str, gid: str) -> discord.Embed:
    prefs    = state.get_user_prefs(uid, gid)
    tz_name  = _user_tz_str(uid)
    try:
        tz       = ZoneInfo(tz_name)
        local    = datetime.now(tz).strftime("%H:%M")
        tz_line  = f"🕐 **Your timezone:** {tz_name}  *(local time: {local})*"
    except Exception:
        tz_line  = f"🕐 **Your timezone:** {tz_name}"

    em = discord.Embed(
        title="🔔 My Notification Settings",
        description=(
            "Toggle which World Cup notifications you receive.\n"
            "**ON** = receive  |  **OFF** = skip\n"
            "*All ON = follow the server's default mode.*\n\n"
            + tz_line
        ),
        color=emb.C_BLUE,
    )
    rows = [
        f"{'✅' if prefs.get(k, True) else '❌'}  {label}"
        for k, label in _USER_SETTING_LABELS.items()
    ]
    em.add_field(name="Notifications", value="\n".join(rows), inline=False)
    em.set_footer(text="Changes apply immediately • Only visible to you • Use the timezone dropdown to change your timezone")
    return em


# ── Follow team view ──────────────────────────────────────────────────────────

class UnfollowTeamView(discord.ui.View):
    def __init__(self, uid: str):
        super().__init__(timeout=120)
        self.uid = uid
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        following = state.get_user_following(self.uid)
        teams = following.get("teams", [])
        if teams:
            options = [discord.SelectOption(label=t[:100], value=t[:100]) for t in teams[:25]]
            select  = discord.ui.Select(
                placeholder="Select a team to unfollow…",
                min_values=1, max_values=1, options=options,
            )
            select.callback = self._on_unfollow
            self.add_item(select)

    async def _on_unfollow(self, i: discord.Interaction) -> None:
        team_name = i.data["values"][0]
        following = dict(state.get_user_following(self.uid))
        following["teams"] = [t for t in following.get("teams", []) if t != team_name]
        state.set_user_following(self.uid, following)
        self._rebuild()
        em = _build_teams_embed(self.uid)
        await i.response.edit_message(embed=em, view=self if following.get("teams") else None)


def _build_teams_embed(uid: str) -> discord.Embed:
    following = state.get_user_following(uid)
    teams = following.get("teams", [])
    em = discord.Embed(title="⭐ My Favourite National Teams", color=emb.C_GREEN)
    if teams:
        em.description = "\n".join(f"• {team_flag({'name': t})} {t}" for t in teams)
    else:
        em.description = (
            "You're not following any national teams yet.\n"
            "Use `/followteam <name>` to start — e.g. `/followteam Brazil`"
        )
    em.set_footer(text="You'll get a personal ping when your followed teams play")
    return em


# ── Dashboard panel ───────────────────────────────────────────────────────────

class DashboardView(discord.ui.View):
    """Persistent interactive dashboard — all ephemeral responses."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚽ Live Matches",  style=discord.ButtonStyle.danger,     custom_id="dash_live",   row=0)
    async def dash_live(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        try:
            matches = await get_live_matches()
            await i.followup.send(embed=emb.embed_live(_annotate_matches(matches)), ephemeral=True)
        except Exception as e:
            log.error("[DASHBOARD] live error: %s", e)
            await i.followup.send(embed=_error_embed("Could not load live matches."), ephemeral=True)

    @discord.ui.button(label="📅 Schedule",       style=discord.ButtonStyle.secondary,  custom_id="dash_sched",  row=0)
    async def dash_sched(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        try:
            matches = await get_todays_matches()
            embeds  = emb.embed_today(_annotate_matches(matches))
            if embeds:
                await i.followup.send(embed=embeds[0], ephemeral=True)
            else:
                await i.followup.send(
                    embed=discord.Embed(description="📭 No matches today.", color=emb.C_GREY),
                    ephemeral=True,
                )
        except Exception as e:
            log.error("[DASHBOARD] sched error: %s", e)
            await i.followup.send(embed=_error_embed("Could not load today's schedule."), ephemeral=True)

    @discord.ui.button(label="🏆 Standings",      style=discord.ButtonStyle.primary,    custom_id="dash_stand",  row=0)
    async def dash_stand(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        try:
            data = await get_standings()
        except Exception as e:
            log.error("[DASHBOARD] standings error: %s", e)
            await i.followup.send(embed=_error_embed("Could not load standings."), ephemeral=True)
            return
        if not data:
            await i.followup.send(
                embed=_error_embed(
                    "Standings not yet available.",
                    "Standings appear once the group stage begins. Try `/upcoming` for the schedule."
                ),
                ephemeral=True,
            )
            return
        tables = data.get("standings", [])
        if tables:
            g   = tables[0].get("group", "A").replace("GROUP_", "")
            em  = emb.embed_wc_group(g, tables[0].get("table", []))
        else:
            em = discord.Embed(description="No standings data available.", color=emb.C_GREY)
        await i.followup.send(embed=em, ephemeral=True)

    @discord.ui.button(label="🎯 Predictions",    style=discord.ButtonStyle.primary,    custom_id="dash_pred",   row=1)
    async def dash_pred(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.guild:
            await i.response.send_message("Use this in a server.", ephemeral=True)
            return
        uid   = str(i.user.id)
        gid   = str(i.guild_id)
        stats = state.get_prediction_stats(uid, gid)
        lb    = state.get_leaderboard(gid)
        sorted_lb = sorted(lb.items(), key=lambda x: x[1].get("points", 0), reverse=True)
        rank  = next((r + 1 for r, (u, _) in enumerate(sorted_lb) if u == uid), None)
        em    = emb.embed_prediction_stats(i.user, stats, rank)
        await i.response.send_message(embed=em, ephemeral=True)

    @discord.ui.button(label="⭐ Favourites",     style=discord.ButtonStyle.secondary,  custom_id="dash_fav",    row=1)
    async def dash_fav(self, i: discord.Interaction, b: discord.ui.Button):
        uid  = str(i.user.id)
        em   = _build_teams_embed(uid)
        view = UnfollowTeamView(uid) if state.get_user_following(uid).get("teams") else None
        await i.response.send_message(embed=em, view=view, ephemeral=True)

    @discord.ui.button(label="🔔 Notifications",  style=discord.ButtonStyle.secondary,  custom_id="dash_notif",  row=1)
    async def dash_notif(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.guild:
            await i.response.send_message("Use this in a server.", ephemeral=True)
            return
        uid  = str(i.user.id)
        gid  = str(i.guild_id)
        view = UserSettingsView(uid, gid)
        await i.response.send_message(
            embed=_build_settings_embed(uid, gid), view=view, ephemeral=True
        )

    @discord.ui.button(label="⚙️ Settings",       style=discord.ButtonStyle.secondary,  custom_id="dash_cfg",    row=1)
    async def dash_cfg(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.guild:
            await i.response.send_message("Use this in a server.", ephemeral=True)
            return
        gid = str(i.guild_id)
        cfg = state.get_guild_config(gid)
        em  = emb.embed_status(i.guild, cfg, monitor_loop.is_running())
        await i.response.send_message(embed=em, ephemeral=True)


# ── Interactive panel ─────────────────────────────────────────────────────────

class InteractivePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔔 My Notifications", style=discord.ButtonStyle.primary,   custom_id="ip_notif",  row=0)
    async def btn_notif(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.guild:
            await i.response.send_message("Use this in a server.", ephemeral=True)
            return
        uid = str(i.user.id)
        gid = str(i.guild_id)
        view = UserSettingsView(uid, gid)
        await i.response.send_message(embed=_build_settings_embed(uid, gid), view=view, ephemeral=True)

    @discord.ui.button(label="⭐ My Teams",          style=discord.ButtonStyle.secondary, custom_id="ip_teams",  row=0)
    async def btn_teams(self, i: discord.Interaction, b: discord.ui.Button):
        uid  = str(i.user.id)
        em   = _build_teams_embed(uid)
        view = UnfollowTeamView(uid) if state.get_user_following(uid).get("teams") else None
        await i.response.send_message(embed=em, view=view, ephemeral=True)

    @discord.ui.button(label="🏅 Leaderboard",       style=discord.ButtonStyle.secondary, custom_id="ip_lb",     row=0)
    async def btn_lb(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.guild:
            await i.response.send_message("Use this in a server.", ephemeral=True)
            return
        gid = str(i.guild_id)
        lb  = state.get_leaderboard(gid)
        await i.response.send_message(embed=emb.embed_leaderboard(i.guild, lb), ephemeral=True)

    @discord.ui.button(label="📅 Today's Matches",   style=discord.ButtonStyle.secondary, custom_id="ip_today",  row=1)
    async def btn_today(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        try:
            matches = await get_todays_matches()
            embeds  = emb.embed_today(_annotate_matches(matches))
            if embeds:
                await i.followup.send(embed=embeds[0], ephemeral=True)
            else:
                await i.followup.send(
                    embed=discord.Embed(description="📭 No matches today.", color=emb.C_GREY),
                    ephemeral=True,
                )
        except Exception as e:
            log.error("[PANEL] Today error: %s", e)
            await i.followup.send(
                embed=_error_embed("Could not load today's matches.", "Try `/today` instead."),
                ephemeral=True,
            )

    @discord.ui.button(label="📊 My Stats",          style=discord.ButtonStyle.secondary, custom_id="ip_stats",  row=1)
    async def btn_stats(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.guild:
            await i.response.send_message("Use this in a server.", ephemeral=True)
            return
        uid   = str(i.user.id)
        gid   = str(i.guild_id)
        stats = state.get_prediction_stats(uid, gid)
        lb    = state.get_leaderboard(gid)
        sorted_lb = sorted(lb.items(), key=lambda x: x[1].get("points", 0), reverse=True)
        rank  = next((r + 1 for r, (u, _) in enumerate(sorted_lb) if u == uid), None)
        await i.response.send_message(
            embed=emb.embed_prediction_stats(i.user, stats, rank), ephemeral=True
        )


# ── Help navigation ───────────────────────────────────────────────────────────

class HelpView(discord.ui.View):
    """Paginated help browser — one category per page."""

    def __init__(self, page: int = 0):
        super().__init__(timeout=120)
        self.page = page
        self._sync()

    def _sync(self) -> None:
        cats  = list(COMMAND_REGISTRY.keys())
        total = len(cats)
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= total - 1
        self.page_label.label  = f"{self.page + 1} / {total}"

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, i: discord.Interaction, b: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._sync()
        await i.response.edit_message(embed=emb.embed_help(COMMAND_REGISTRY, self.page), view=self)

    @discord.ui.button(label="1 / 5", style=discord.ButtonStyle.secondary, row=0, disabled=True)
    async def page_label(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, i: discord.Interaction, b: discord.ui.Button):
        cats  = list(COMMAND_REGISTRY.keys())
        self.page = min(len(cats) - 1, self.page + 1)
        self._sync()
        await i.response.edit_message(embed=emb.embed_help(COMMAND_REGISTRY, self.page), view=self)


# ── Setup wizard ──────────────────────────────────────────────────────────────

class SetupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, cfg_key: str, label: str, placeholder: str):
        self.cfg_key    = cfg_key
        self.step_label = label
        super().__init__(
            placeholder=placeholder,
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=0,
        )

    async def callback(self, i: discord.Interaction) -> None:
        gid = str(i.guild_id)
        if self.values:
            cid = str(self.values[0].id)
            state.set_channel_id(gid, self.cfg_key, cid)
            if self.cfg_key == "commands_channel_id":
                state.set_channel_id(gid, "interactive_channel_id", cid)
                await _post_commands_menu_if_needed()
                await _post_or_update_interactive_panel(gid, cid)
        view = self.view
        if isinstance(view, SetupWizardView):
            view.step += 1
            await view.update(i)


class SetupWizardView(discord.ui.View):
    """
    Multi-step setup wizard.
    Steps: 0=live 1=predictions 2=results 3=summary 4=timezone 5=mode 6=done
    """

    STEPS = [
        ("Live Alerts Channel",            "channel_id",             "📡 Select your live match alerts channel"),
        ("Predictions & Results Channel",  "predictions_channel_id", "🎯 Select your predictions, MOTM & results channel"),
        ("Match Recaps Channel",           "summary_channel_id",     "📺 Select your match recaps & highlights channel"),
    ]

    def __init__(self, step: int = 0):
        super().__init__(timeout=300)
        if not any(s[1] == "commands_channel_id" for s in self.STEPS):
            self.STEPS.append((
                "Commands & Interactive Channel",
                "commands_channel_id",
                "Select your commands and self-service panel channel",
            ))
        self.step = step
        self._build_step()

    def _build_step(self) -> None:
        self.clear_items()

        if self.step < len(self.STEPS):
            label, key, placeholder = self.STEPS[self.step]
            self.add_item(SetupChannelSelect(key, label, placeholder))
            skip = discord.ui.Button(label="⏭ Skip", style=discord.ButtonStyle.secondary, row=1)
            skip.callback = self._skip
            self.add_item(skip)

        elif self.step == len(self.STEPS):  # timezone
            tz_select = discord.ui.Select(
                placeholder="🕐 Select your server timezone…",
                min_values=1, max_values=1,
                options=[
                    discord.SelectOption(label="UTC",                       value="UTC"),
                    discord.SelectOption(label="US Eastern (UTC-5/-4)",     value="America/New_York"),
                    discord.SelectOption(label="US Central (UTC-6/-5)",     value="America/Chicago"),
                    discord.SelectOption(label="US Pacific (UTC-8/-7)",     value="America/Los_Angeles"),
                    discord.SelectOption(label="UK / Ireland (UTC+0/+1)",   value="Europe/London"),
                    discord.SelectOption(label="Central Europe (UTC+1/+2)", value="Europe/Berlin"),
                    discord.SelectOption(label="Brazil (UTC-3)",            value="America/Sao_Paulo"),
                    discord.SelectOption(label="Mexico City (UTC-6/-5)",    value="America/Mexico_City"),
                    discord.SelectOption(label="Argentina (UTC-3)",         value="America/Argentina/Buenos_Aires"),
                    discord.SelectOption(label="Japan (UTC+9)",             value="Asia/Tokyo"),
                    discord.SelectOption(label="Australia / Sydney",        value="Australia/Sydney"),
                    discord.SelectOption(label="India (UTC+5:30)",          value="Asia/Kolkata"),
                ],
                row=0,
            )
            tz_select.callback = self._tz_callback
            self.add_item(tz_select)
            skip = discord.ui.Button(label="⏭ Skip (keep UTC)", style=discord.ButtonStyle.secondary, row=1)
            skip.callback = self._skip
            self.add_item(skip)

        elif self.step == len(self.STEPS) + 1:  # mode
            mode_select = discord.ui.Select(
                placeholder="🔔 Select notification verbosity…",
                min_values=1, max_values=1,
                options=[
                    discord.SelectOption(label="🔇 Quiet — Goals + Red Cards + Full Time only",     value="quiet"),
                    discord.SelectOption(label="📢 Standard — + HT, ET, MOTM Polls, Predictions",   value="standard"),
                    discord.SelectOption(label="📋 Detailed — Everything: Lineups, Recaps, Tables",  value="detailed"),
                ],
                row=0,
            )
            mode_select.callback = self._mode_callback
            self.add_item(mode_select)
            skip = discord.ui.Button(label="⏭ Skip (keep Standard)", style=discord.ButtonStyle.secondary, row=1)
            skip.callback = self._skip
            self.add_item(skip)

    async def _skip(self, i: discord.Interaction) -> None:
        self.step += 1
        await self.update(i)

    async def _tz_callback(self, i: discord.Interaction) -> None:
        tz_name = i.data["values"][0]
        gid     = str(i.guild_id)
        cfg     = state.get_guild_config(gid)
        cfg["timezone"] = tz_name
        state.set_guild_config(gid, cfg)
        self.step += 1
        await self.update(i)

    async def _mode_callback(self, i: discord.Interaction) -> None:
        mode = i.data["values"][0]
        gid  = str(i.guild_id)
        cfg  = state.get_guild_config(gid)
        cfg["mode"] = mode
        state.set_guild_config(gid, cfg)
        self.step += 1
        await self.update(i)

    async def update(self, i: discord.Interaction) -> None:
        total = len(self.STEPS) + 2
        if self.step >= total:
            gid = str(i.guild_id)
            cfg = state.get_guild_config(gid)
            await i.response.edit_message(
                embed=emb.embed_setup_complete(cfg, i.guild), view=None
            )
            return
        self._build_step()
        step_name = (
            self.STEPS[self.step][0]
            if self.step < len(self.STEPS)
            else ("Timezone" if self.step == len(self.STEPS) else "Notification Mode")
        )
        em = discord.Embed(
            title=f"⚙️ Setup — Step {self.step + 1}/{total}",
            description=f"**{step_name}**\nUse the selector below, or click **Skip** to leave unchanged.",
            color=emb.C_GOLD,
        )
        em.set_footer(text="🏆 FIFA World Cup 2026 Bot Setup")
        await i.response.edit_message(embed=em, view=self)


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND MONITOR LOOP
# ═══════════════════════════════════════════════════════════════════════════════

_monitor_tick: int = 0          # counts loop iterations for periodic tasks
_cached_today_matches: list     = []   # refreshed every ~2 min to save API quota
_cached_today_date:   str       = ""   # UTC date the cache was built for
_last_monitor_tick_at: datetime | None = None
# req #19: dedup guard — key="{mid}:{event_key}" → last-sent UTC timestamp (float)
_recent_sends: dict[str, float] = {}


@tasks.loop(seconds=30)
async def monitor_loop() -> None:
    """Poll for live match events every 30 seconds.

    Rate-limit arithmetic (football-data.org free tier = 10 req/min):
      • get_live_matches()          — 1 call  (was 2; halved by the LIVE alias fix)
      • get_competition_matches()   — 1 call  (cached; only runs every 4th tick = every 2 min)
      • get_match_detail() × N      — 1 call per live match
      Worst case with 3 simultaneous live matches: 5 calls / 30 s = 10 calls/min  ✓
    """
    global _monitor_tick, _cached_today_matches, _cached_today_date, _last_monitor_tick_at
    _monitor_tick += 1
    _last_monitor_tick_at = datetime.now(timezone.utc)

    # req #19: per-event dedup guard (key = "{mid}:{event_key}", value = timestamp)
    # Declared here so the type-checker sees it; value is populated as events are sent.

    # Refresh WC match order every 60 min (120 ticks × 30 s = 3 600 s)
    if _monitor_tick % 120 == 1:
        try:
            await load_wc_match_order()
        except Exception as e:
            log.warning("[MONITOR] Match-order refresh failed: %s", e)

    configs = state.all_guild_configs()
    if not configs:
        return

    try:
        live_matches = await get_live_matches()
    except Exception as e:
        log.error("[MONITOR] Failed to fetch live matches: %s", e)
        return

    # Refresh the today's schedule cache every 4 ticks (2 min) or when the UTC
    # date rolls over.  Reminder logic only needs minute-level accuracy so this
    # is more than fresh enough and cuts one API call per tick.
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _monitor_tick % 4 == 1 or _cached_today_date != today_str:
        try:
            _cached_today_matches = await get_competition_matches(
                today_str,
                (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d"),
            )
            _cached_today_date = today_str
        except Exception as e:
            log.error("[MONITOR] Failed to fetch today's matches: %s", e)
            if not _cached_today_matches:
                _cached_today_matches = []

    all_wc = _cached_today_matches

    now = datetime.now(timezone.utc)

    # — Reminders for upcoming matches —
    for match in all_wc:
        mid = str(match["id"])
        dt  = parse_dt(match.get("utcDate", ""))
        if not dt:
            continue
        diff = (dt - now).total_seconds() / 60

        for minutes in (60, 15):
            # Fire once the threshold is reached. The lower bound tolerates a
            # few minutes of loop delay (rate-limit back-off, slow API) so a
            # reminder is never silently skipped, while never firing after
            # kick-off (diff > 0 is guaranteed since minutes-5 > 0).
            if minutes - 5 < diff <= minutes and not state.is_reminder_sent(mid, minutes):
                state.mark_reminder_sent(mid, minutes)
                log.info("[MONITOR] Reminder %d min for match %s (%.1f min out)", minutes, mid, diff)
                ann           = _annotate_match(match)
                reminder_em   = emb.embed_reminder(ann, minutes)

                if minutes == 15:
                    # 15-min reminder: main live channel + thread (req #13: no mode gate)
                    await broadcast(reminder_em)
                    await broadcast_to_threads(ann, embed=reminder_em)
                else:
                    # 60-min pre-match summary: thread only (not main live channel)
                    await broadcast_to_threads(ann, embed=reminder_em)

                # At the 60-min reminder, also send confirmed lineups to thread if available.
                # Using the same "LINEUP" sent-key as kick-off so we never double-send.
                if minutes == 60:
                    try:
                        detail_pre = await get_match_detail(match["id"])
                        if detail_pre and has_confirmed_lineups(detail_pre):
                            if "LINEUP" not in state.get_sent(mid):
                                state.mark_sent(mid, "LINEUP")
                                log.info("[MONITOR] Lineups confirmed at 60-min reminder for match %s", mid)
                                await broadcast_to_threads(
                                    ann,
                                    embed=emb.embed_lineups(ann, detail_pre),
                                )
                    except Exception as _e:
                        log.warning("[MONITOR] Could not fetch lineups at 60-min for match %s: %s", mid, _e)

        # Prediction poll — ~90 min before kickoff (req #13: no mode gate)
        if 85 < diff <= 92 and not state.is_reminder_sent(mid, 90):
            state.mark_reminder_sent(mid, 90)
            home = team_display(match.get("homeTeam", {}))
            away = team_display(match.get("awayTeam", {}))
            ko   = is_knockout(match)
            em_pred = emb.embed_prediction_poll(_annotate_match(match))
            view = PredictionView(mid, home, away, ko)
            for gid, cfg in configs.items():
                msg = await _send_to_channel_return(gid, cfg, "predictions_channel_id", embed=em_pred, view=view)
                if msg:
                    state.save_prediction_poll_message(mid, gid, msg.id)
            log.info("[MONITOR] Prediction poll posted for match %s", mid)

    # — Process live matches —
    # Also catch full-time for matches that dropped out of the live feed between polls
    live_ids = {str(m["id"]) for m in live_matches}
    for match in all_wc:
        mid = str(match["id"])
        if mid in live_ids:
            continue  # handled in the live loop below
        if match.get("status") != "FINISHED":
            continue
        if "FT" in state.get_sent(mid):
            continue
        snap = state.get_snapshot(mid)
        # Only act if the bot was tracking this match while it was live
        if snap.get("status") not in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
            continue
        state.mark_sent(mid, "FT")
        detail = await get_match_detail(match["id"])
        log.info("[MONITOR] FT (missed transition) for match %s (was %s)", mid, snap.get("status"))
        await _process_fulltime(match, detail)
        state.set_snapshot(mid, {
            "home_score": get_score(detail or match, "fullTime")[0],
            "away_score": get_score(detail or match, "fullTime")[1],
            "status": "FINISHED",
        })

    for match in live_matches:
        mid      = str(match["id"])
        snap     = state.get_snapshot(mid)
        cur_stat = match.get("status", "")
        prev_stat = snap.get("status", "")
        stat_changed = cur_stat != prev_stat

        # Kickoff
        if stat_changed and cur_stat == "IN_PLAY" and prev_stat not in ("IN_PLAY", "PAUSED"):
            sent = state.get_sent(mid)
            if "KO" not in sent:
                state.mark_sent(mid, "KO")
                state.lock_predictions(mid)  # marks all predictions locked + closes window
                detail = await get_match_detail(match["id"])
                ann_ko = _annotate_match(match)
                ko_em  = emb.embed_kickoff(ann_ko, detail or {})
                log.info("[MONITOR] Kick-off for match %s", mid)
                # Kickoff: main live channel + thread
                await broadcast(ko_em, min_mode="quiet")
                await broadcast_to_threads(ann_ko, min_mode="quiet", embed=ko_em)

                # Edit prediction poll to show locked footer and remove interactive buttons
                for gid, cfg in state.all_guild_configs().items():
                    poll_msg_id = state.get_prediction_poll_message(mid, gid)
                    if not poll_msg_id:
                        continue
                    cid = cfg.get("predictions_channel_id")
                    if not cid:
                        continue
                    ch = bot.get_channel(int(cid))
                    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                        continue
                    try:
                        poll_msg = await ch.fetch_message(int(poll_msg_id))
                        locked_em = poll_msg.embeds[0].copy() if poll_msg.embeds else None
                        if locked_em:
                            locked_em.set_footer(text="🔒 Predictions locked — match has started  •  🏆 FIFA World Cup 2026")
                        await poll_msg.edit(embed=locked_em, view=None)
                    except discord.HTTPException:
                        pass

                # Lineups: thread only (not main live channel)
                if detail and has_confirmed_lineups(detail) and "LINEUP" not in sent:
                    state.mark_sent(mid, "LINEUP")
                    await broadcast_to_threads(ann_ko, min_mode="detailed",
                                               embed=emb.embed_lineups(ann_ko, detail))

        await _process_live_match(match, snap, stat_changed)

        # FT: fire whenever status is FINISHED and we haven't sent it yet.
        # Removed the `stat_changed` guard — the bot can miss a single poll
        # transition (rate-limit, restart) so we recheck every cycle.
        if cur_stat == "FINISHED" and "FT" not in state.get_sent(mid):
            state.mark_sent(mid, "FT")
            detail = await get_match_detail(match["id"])
            log.info("[MONITOR] Full time for match %s", mid)
            await _process_fulltime(match, detail)


async def _process_live_match(match: dict, snap: dict, status_changed: bool) -> None:
    mid    = str(match["id"])
    detail = await get_match_detail(match["id"])
    if not detail:
        return

    goals        = detail.get("goals") or []
    bookings     = detail.get("bookings") or []
    red_cards    = [b for b in bookings if b.get("card") in ("RED", "YELLOW_RED")]
    yellow_cards = [b for b in bookings if b.get("card") == "YELLOW"]
    cur_stat     = match.get("status", "")
    h, a         = get_current_score(match)
    ann_match    = _annotate_match(match)

    # Goals → live-matches channel + thread (spec: goals belong in both)
    announced = state.get_announced_goals(mid)
    for goal in goals:
        gk = goal_key(goal)
        if gk not in announced:
            state.announce_goal(mid, gk)
            scorer = (goal.get("scorer") or {}).get("name", "")
            log.info("[GOAL] Match %s — %s' %s", mid, goal.get("minute", "?"), scorer)
            goal_em = emb.embed_goal(ann_match, detail, goal)
            await broadcast(goal_em)                              # live-matches channel
            await broadcast_to_threads(ann_match, embed=goal_em) # match thread

    # Red cards → live-matches channel + thread (spec: red cards belong in both)
    announced_c = state.get_announced_cards(mid)
    for card in red_cards:
        ck = card_key(card)
        if ck not in announced_c:
            state.announce_card(mid, ck)
            player = (card.get("player") or {}).get("name", "?")
            log.info("[CARD] Match %s — %s' %s (red)", mid, card.get("minute", "?"), player)
            red_em = emb.embed_red_card(ann_match, detail, card)
            await broadcast(red_em)                              # live-matches channel
            await broadcast_to_threads(ann_match, embed=red_em) # match thread

    # Yellow cards → thread only (all guilds — req #13: mode gate removed)
    for card in yellow_cards:
        ck = card_key(card)
        if ck not in announced_c:
            state.announce_card(mid, ck)
            player = (card.get("player") or {}).get("name", "?")
            log.info("[CARD] Match %s — %s' %s (yellow)", mid, card.get("minute", "?"), player)
            await broadcast_to_threads(ann_match, embed=emb.embed_yellow_card(ann_match, detail, card))

    # Status transitions → thread only
    announced_s = state.get_announced_subs(mid)
    for sub in _extract_substitutions(detail):
        sk = _sub_key(sub)
        if sk not in announced_s:
            state.announce_sub(mid, sk)
            log.info(
                "[SUB] Match %s - %s' %s on, %s off",
                mid, sub.get("minute", "?"), sub.get("player_in", "?"), sub.get("player_out", "?"),
            )
            sub_embed = emb.embed_substitution(ann_match, detail, sub)
            sent_to_thread = await broadcast_to_threads(ann_match, embed=sub_embed)
            if not sent_to_thread:
                await broadcast_predictions(sub_embed)

    sent      = state.get_sent(mid)
    prev_stat = snap.get("status", "")

    if status_changed:
        if cur_stat == "PAUSED" and "HT" not in sent:
            state.mark_sent(mid, "HT")
            log.info("[MONITOR] Half-time for match %s", mid)
            await broadcast_to_threads(ann_match, embed=emb.embed_halftime(ann_match, detail))

        elif cur_stat == "IN_PLAY" and prev_stat == "PAUSED" and "2H" not in sent:
            state.mark_sent(mid, "2H")
            log.info("[MONITOR] Second half for match %s", mid)
            await broadcast_to_threads(ann_match, embed=emb.embed_second_half(ann_match))

        elif cur_stat == "EXTRA_TIME" and "ET" not in sent:
            state.mark_sent(mid, "ET")
            log.info("[MONITOR] Extra time for match %s", mid)
            await broadcast_to_threads(ann_match, embed=emb.embed_extra_time(ann_match, detail))

        elif cur_stat == "PENALTY_SHOOTOUT" and "PSO" not in sent:
            state.mark_sent(mid, "PSO")
            log.info("[MONITOR] Penalty shootout for match %s", mid)
            pso_em = emb.embed_penalty_shootout(ann_match, detail)
            await broadcast(pso_em)                              # live-matches channel (spec)
            await broadcast_to_threads(ann_match, embed=pso_em) # match thread

        # SUSPENDED / CANCELLED / POSTPONED — notify once and stop tracking
        elif cur_stat in ("SUSPENDED", "CANCELLED", "POSTPONED") and cur_stat not in sent:
            state.mark_sent(mid, cur_stat)
            log.info("[MONITOR] Match %s status: %s", mid, cur_stat)
            label_map = {
                "SUSPENDED":  "⚠️ Match Suspended",
                "CANCELLED":  "❌ Match Cancelled",
                "POSTPONED":  "📅 Match Postponed",
            }
            alert_em = discord.Embed(
                title=label_map.get(cur_stat, cur_stat),
                description=f"**{team_display(ann_match.get('homeTeam', {}))} vs {team_display(ann_match.get('awayTeam', {}))}** — official update.",
                color=emb.C_ORANGE if cur_stat == "SUSPENDED" else emb.C_RED,
            )
            await broadcast(alert_em)
            await broadcast_to_threads(ann_match, embed=alert_em)

    # MOTM vote — fires between 60–70 min (req #5); outside status_changed so it fires even when status stays IN_PLAY
    # Spec: "Select the top 8 player candidates from both teams using ratings/statistics when available."
    minute = get_minute(match) or get_minute(detail) or 0
    if cur_stat == "IN_PLAY" and 60 <= minute < 70 and "MOTM" not in sent:
        nominees = _build_motm_nominees(detail)
        if not nominees:
            nominees = _build_motm_fallback(match)
        nominees = nominees[:8]  # spec: top 8 candidates only; never team names
        if nominees:
            state.mark_sent(mid, "MOTM")
            log.info("[MONITOR] MOTM vote for match %s at %d' — %d nominees: %s",
                     mid, minute, len(nominees), ", ".join(nominees))
            motm_em   = emb.embed_motm_vote(ann_match, nominees)
            motm_view = MotmVoteView(mid, nominees)
            # req #13: mode gate removed — all guilds receive MOTM vote in predictions channel
            for gid, cfg in state.all_guild_configs().items():
                msg = await _send_to_channel_return(gid, cfg, "predictions_channel_id", embed=motm_em, view=motm_view)
                if msg:
                    state.save_motm_message_id(mid, gid, msg.id)
                    log.info("[MONITOR] MOTM poll posted to predictions channel guild %s msg_id=%s", gid, msg.id)
        else:
            # No nominees at all — mark sent so we don't retry on every loop tick
            log.warning("[MONITOR] No MOTM nominees for match %s at %d min — skipping vote", mid, minute)
            state.mark_sent(mid, "MOTM")

    state.set_snapshot(mid, {"home_score": h, "away_score": a, "status": cur_stat})


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _player_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "shortName", "displayName", "fullName"):
            name = value.get(key)
            if isinstance(name, str) and name.strip():
                return name.strip()
        player = value.get("player")
        if player is not value:
            return _player_name(player)
    return ""


def _team_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("shortName", "name", "tla"):
            name = value.get(key)
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


def _minute_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("minute") or value.get("elapsed") or value.get("display")
    if value is None:
        return "?"
    return str(value).replace("'", "").strip() or "?"


def _sub_key(sub: dict) -> str:
    return "|".join(
        str(sub.get(k, "")).strip().lower()
        for k in ("minute", "team", "player_in", "player_out")
    )


def _extract_substitutions(detail: dict | None) -> list[dict]:
    """Return normalized substitutions from several known/likely API shapes."""
    if not isinstance(detail, dict):
        return []

    raw_items: list[dict] = []
    for key in ("substitutions", "subs"):
        raw_items.extend([x for x in _as_list(detail.get(key)) if isinstance(x, dict)])

    for key in ("events", "timeline", "incidents"):
        for event in _as_list(detail.get(key)):
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or event.get("eventType") or event.get("kind") or "").upper()
            if "SUB" in event_type:
                raw_items.append(event)

    for lineup in _as_list(detail.get("lineups")):
        if not isinstance(lineup, dict):
            continue
        team = lineup.get("team")
        for key in ("substitutions", "subs"):
            for item in _as_list(lineup.get(key)):
                if isinstance(item, dict):
                    merged = dict(item)
                    merged.setdefault("team", team)
                    raw_items.append(merged)

    seen: set[str] = set()
    subs: list[dict] = []
    for item in raw_items:
        player_in = (
            _player_name(item.get("playerIn")) or _player_name(item.get("in")) or
            _player_name(item.get("substitute")) or _player_name(item.get("replacement")) or
            _player_name(item.get("player_on")) or _player_name(item.get("playerOn"))
        )
        player_out = (
            _player_name(item.get("playerOut")) or _player_name(item.get("out")) or
            _player_name(item.get("replacedPlayer")) or _player_name(item.get("playerOff")) or
            _player_name(item.get("player_off"))
        )
        if not player_in and not player_out and str(item.get("type") or "").upper().startswith("SUB"):
            player_in = _player_name(item.get("player"))
            player_out = _player_name(item.get("assist")) or _player_name(item.get("relatedPlayer"))
        if not player_in and not player_out:
            log.debug("[SUB] Unknown substitution shape: %s", item)
            continue
        sub = {
            "minute": _minute_value(item.get("minute") or item.get("time")),
            "team": _team_name(item.get("team")),
            "player_in": player_in or "Player on",
            "player_out": player_out or "Player off",
        }
        key = _sub_key(sub)
        if key not in seen:
            seen.add(key)
            subs.append(sub)
    return subs


def _build_motm_fallback(match: dict) -> list[str]:
    """Build a MOTM nominee list from squad data only (req #3: no team-name fallback).

    Returns an empty list when no player names are available — the caller will
    then skip the MOTM poll entirely rather than posting a team-name-only ballot.
    """
    nominees: list[str] = []
    for team_key in ("homeTeam", "awayTeam"):
        team = match.get(team_key) or {}
        squad = team.get("squad") or []
        for player in squad[:5]:
            name = (player.get("name") or "").strip()
            if name and name not in nominees:
                nominees.append(name)
    # req #3: do NOT add team display names as last resort — return [] if no players found
    return nominees[:10]


def _build_motm_nominees(detail: dict) -> list[str]:
    # req #16: dead duplicate removed (was above this function)
    # This is the only _build_motm_nominees implementation.
    """Build MOTM nominees from ratings first, then robust match data fallbacks."""
    if not isinstance(detail, dict):
        return []

    def add_unique(names: list[str], name: str) -> None:
        clean = name.strip()
        if clean and clean not in names:
            names.append(clean)

    rated: dict[str, float] = {}

    def scan_ratings(value: Any) -> None:
        if isinstance(value, dict):
            name = _player_name(value.get("player")) or _player_name(value)
            rating = (
                value.get("rating")
                or value.get("score")
                or value.get("matchRating")
                or (value.get("statistics") or {}).get("rating")
            )
            try:
                if name and rating is not None:
                    rated[name] = max(rated.get(name, 0.0), float(rating))
            except (TypeError, ValueError):
                pass
            for child in value.values():
                if isinstance(child, (dict, list)):
                    scan_ratings(child)
        elif isinstance(value, list):
            for child in value:
                scan_ratings(child)

    for key in ("ratings", "playerRatings", "players", "statistics", "lineups"):
        scan_ratings(detail.get(key))
    if rated:
        return [name for name, _ in sorted(rated.items(), key=lambda item: item[1], reverse=True)[:10]]

    fallback: list[str] = []
    for lineup in _as_list(detail.get("lineups")):
        if not isinstance(lineup, dict):
            continue
        for key in ("startXI", "startingXI", "starters", "startingLineup"):
            for entry in _as_list(lineup.get(key)):
                if isinstance(entry, dict):
                    add_unique(fallback, _player_name(entry.get("player")) or _player_name(entry))
                else:
                    add_unique(fallback, _player_name(entry))

    for sub in _extract_substitutions(detail):
        add_unique(fallback, sub.get("player_in", ""))

    for goal in _as_list(detail.get("goals")):
        if isinstance(goal, dict):
            if str(goal.get("type") or "").upper() != "OWN":
                add_unique(fallback, _player_name(goal.get("scorer")))
            add_unique(fallback, _player_name(goal.get("assist")))

    for card in _as_list(detail.get("bookings")):
        if isinstance(card, dict):
            add_unique(fallback, _player_name(card.get("player")))

    if fallback:
        return fallback[:30]

    # req #3: do NOT fall back to team names — return [] when no player data is available
    return []


async def _process_fulltime(match: dict, detail: dict | None) -> None:
    mid = str(match["id"])
    log.info("[MONITOR] Processing full time for match %s", mid)

    if detail is None:
        log.warning("[MONITOR] No detail for FT match %s — using basic data", mid)

    ann_match = _annotate_match(match)
    ft_em = emb.embed_fulltime(ann_match, detail or match)
    # Full time: main live channel + thread
    await broadcast(ft_em)
    await broadcast_to_threads(ann_match, embed=ft_em)

    h, a = get_score(detail or match, "fullTime")
    if h is not None and a is not None:
        preds          = state.get_score_predictions(mid)
        exact_winners  = []
        result_winners = []

        for uid, pred in preds.items():
            ph, pa = pred["home"], pred["away"]
            if ph == h and pa == a:
                exact_winners.append((uid, ph, pa))
            elif _same_result(ph, pa, h, a):
                result_winners.append((uid, ph, pa))

        for uid, _, _ in exact_winners:
            for gid in state.all_guild_configs():
                state.update_leaderboard(gid, uid, 3)

        for uid, _, _ in result_winners:
            for gid in state.all_guild_configs():
                state.update_leaderboard(gid, uid, 1)

        if preds:
            log.info("[MONITOR] Prediction results: %d exact, %d result",
                     len(exact_winners), len(result_winners))
            await broadcast_results(
                emb.embed_prediction_results(ann_match, h, a, exact_winners, result_winners)
            )

    # MOTM results — req #4/#14: use official API winner when available
    official_motm: str | None = None
    try:
        official_motm = await get_match_motm(match["id"])
    except Exception as _motm_e:
        log.warning("[MONITOR] Could not fetch official MOTM for match %s: %s", mid, _motm_e)

    for gid, cfg in state.all_guild_configs().items():
        # req #14: guard against double-posting per guild
        if state.get_motm_result_sent(mid, gid):
            continue

        motm_msg_id = state.get_motm_message_id(mid, gid)
        votes       = state.get_motm_votes(mid, gid) or {}
        tally: dict[str, int] = {}
        for uid, player in votes.items():
            tally[player] = tally.get(player, 0) + 1

        if official_motm:
            # req #4: official API winner — only votes for the correct player earn a point
            winners = [official_motm]
            for uid, player in votes.items():
                if player.strip().lower() == official_motm.strip().lower():
                    state.update_leaderboard(gid, uid, 1)
            is_official = True
        elif tally:
            # Community vote tally fallback
            max_v   = max(tally.values())
            winners = [p for p, v in tally.items() if v == max_v]
            for uid, player in votes.items():
                if player in winners:
                    state.update_leaderboard(gid, uid, 1)
            is_official = False
        else:
            continue

        state.mark_motm_result_sent(mid, gid)

        motm_result_em = emb.embed_motm_result(ann_match, winners, tally, is_official=is_official)
        # Post to predictions channel
        cid = cfg.get("predictions_channel_id") or cfg.get("results_channel_id")
        if cid:
            ch = await _resolve_channel(cid)
            if ch:
                try:
                    await ch.send(embed=motm_result_em)
                except discord.HTTPException as _e:
                    log.error("[MONITOR] MOTM result send failed for guild %s: %s", gid, _e)
        # Also post to match thread
        await _send_to_match_thread(ann_match, gid, cfg, embed=motm_result_em)

        # Lock MOTM voting message
        pred_cid = cfg.get("predictions_channel_id")
        if pred_cid and motm_msg_id:
            ch = await _resolve_channel(pred_cid)
            if ch:
                try:
                    motm_msg = await ch.fetch_message(int(motm_msg_id))
                    if motm_msg.embeds:
                        locked_em = motm_msg.embeds[0].copy()
                        locked_em.set_footer(text="🔒 Voting closed  •  🏆 FIFA World Cup 2026")
                        await motm_msg.edit(embed=locked_em, view=None)
                except discord.HTTPException:
                    pass

    # Update leaderboards in predictions channels (pinned; old pin removed)
    for gid, cfg in state.all_guild_configs().items():
        if state.get_leaderboard(gid):
            await _update_leaderboard_in_predictions(gid, cfg)

    asyncio.create_task(_post_delayed_recap(match, 3600))


async def _post_delayed_recap(match: dict, delay: int) -> None:
    await asyncio.sleep(delay)
    mid    = str(match["id"])
    log.info("[MONITOR] Posting recap for match %s", mid)

    # Fetch detail and stats concurrently (get_match_stats internally calls get_match_detail again
    # but that hits the cache; we want the freshest detail for the recap plus structured stats)
    home  = team_display(match.get("homeTeam", {}))
    away  = team_display(match.get("awayTeam", {}))
    query = f"{home} vs {away} World Cup 2026"

    detail, yt, match_stats = await asyncio.gather(
        get_match_detail(match["id"]),
        search_youtube_highlights(query),
        get_match_stats(match["id"]),
        return_exceptions=True,
    )

    # Silently degrade on individual failures
    if isinstance(detail, Exception):
        log.error("[MONITOR] Recap detail fetch failed for %s: %s", mid, detail)
        detail = None
    if isinstance(yt, Exception):
        log.warning("[MONITOR] Recap YouTube fetch failed for %s: %s", mid, yt)
        yt = None
    if isinstance(match_stats, Exception):
        log.warning("[MONITOR] Recap stats fetch failed for %s: %s", mid, match_stats)
        match_stats = None

    ann      = _annotate_match(match)
    recap_em = emb.embed_full_recap(ann, detail, yt, match_stats)
    # Recap goes to both the match thread AND the summary channel
    await broadcast_to_threads(ann, embed=recap_em)
    await broadcast_summary(recap_em)


@monitor_loop.before_loop
async def _before_monitor() -> None:
    await bot.wait_until_ready()
    await asyncio.sleep(3)


# ── Daily summary ──────────────────────────────────────────────────────────────

@tasks.loop(minutes=5)
async def daily_summary_loop() -> None:
    configs = state.all_guild_configs()
    if not configs:
        return

    for gid, cfg in configs.items():
        tz        = _tz_for(gid)
        local_now = datetime.now(tz)
        # Fire in the window 23:50–23:59 in the guild's local timezone
        if local_now.hour != 23 or local_now.minute < 50:
            continue

        date_key = f"{gid}_{local_now.strftime('%Y-%m-%d')}"
        if state.is_daily_summary_sent(date_key):
            continue

        # ── Channel resolution ────────────────────────────────────────────────
        # Standard+ guilds get a leaderboard in the predictions channel;
        # detailed guilds also get the full EOD summary, standings, and bracket.
        summary_cid = cfg.get("summary_channel_id") or cfg.get("channel_id")
        pred_cid    = cfg.get("predictions_channel_id") or cfg.get("channel_id")

        # ── Fetch matches for the guild's LOCAL date (not UTC) ────────────────
        local_date_str = local_now.strftime("%Y-%m-%d")
        try:
            guild_matches = await get_competition_matches(local_date_str, local_date_str)
        except Exception as e:
            log.error("[SUMMARY] Failed to fetch matches for guild %s: %s", gid, e)
            continue

        # Mark sent first so a mid-send error doesn't retry the whole block next tick
        state.mark_daily_summary_sent(date_key)
        date_str = local_now.strftime("%d %B %Y")

        # ── 1. EOD summary embed ──────────────────────────────────────────────
        if summary_cid:
            ch_sum = bot.get_channel(int(summary_cid))
            if isinstance(ch_sum, (discord.TextChannel, discord.Thread)):
                try:
                    eod_msg = await ch_sum.send(
                        embed=emb.embed_daily_summary(_annotate_matches(guild_matches), date_str)
                    )
                    # Pin newest EOD summary, unpin previous one
                    old_eod_id = state.get_pinned_eod_summary(gid)
                    if old_eod_id:
                        try:
                            old_eod = await ch_sum.fetch_message(int(old_eod_id))
                            await old_eod.unpin()
                        except discord.HTTPException:
                            pass
                    try:
                        await eod_msg.pin()
                        state.save_pinned_eod_summary(gid, eod_msg.id)
                    except discord.HTTPException:
                        pass
                    log.info("[SUMMARY] EOD summary sent+pinned for guild %s", gid)
                except discord.HTTPException as e:
                    log.error("[SUMMARY] EOD summary failed for guild %s: %s", gid, e)

        # ── 2. Standings — post all groups, pin ALL messages (req #10) ───────
        if summary_cid:
            ch_sum = bot.get_channel(int(summary_cid))
            if isinstance(ch_sum, (discord.TextChannel, discord.Thread)):
                try:
                    standings_data = await get_standings()
                    if standings_data:
                        # Unpin all previous standings messages (req #10)
                        old_sids = state.get_pinned_standings_ids(gid)
                        for old_sid in old_sids:
                            try:
                                old_smsg = await ch_sum.fetch_message(int(old_sid))
                                await old_smsg.unpin()
                            except discord.HTTPException:
                                pass
                        new_pinned_ids: list[str] = []
                        for table in (standings_data.get("standings") or []):
                            if table.get("type") == "TOTAL":
                                group  = table.get("group", "")
                                letter = group.replace("GROUP_", "") if group else "?"
                                rows   = table.get("table", [])
                                smsg   = await ch_sum.send(embed=emb.embed_wc_group(letter, rows))
                                try:
                                    await smsg.pin()
                                    new_pinned_ids.append(str(smsg.id))
                                except discord.HTTPException:
                                    pass
                        if new_pinned_ids:
                            state.save_pinned_standings_ids(gid, new_pinned_ids)
                            log.info("[SUMMARY] Standings (%d groups) all pinned for guild %s",
                                     len(new_pinned_ids), gid)
                except discord.HTTPException as e:
                    log.error("[SUMMARY] Standings send failed for guild %s: %s", gid, e)
                except Exception as e:
                    log.error("[SUMMARY] Standings fetch failed for guild %s: %s", gid, e)

        # ── 3. Knockout bracket — only when knockout stage has started (req #9) ──
        if summary_cid:
            ch_sum = bot.get_channel(int(summary_cid))
            if isinstance(ch_sum, (discord.TextChannel, discord.Thread)):
                try:
                    ko_matches = await get_competition_matches(
                        "2026-06-01", "2026-07-31", status="SCHEDULED,TIMED,IN_PLAY,FINISHED,PAUSED"
                    )
                    ko_matches = [m for m in ko_matches if is_knockout(m)]
                    # req #9: only post bracket after at least one KO match has played/is playing
                    has_ko_started = any(
                        m.get("status") in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT", "FINISHED")
                        for m in ko_matches
                    )
                    if ko_matches and has_ko_started:
                        bracket_embeds = emb.embed_bracket(ko_matches)
                        for brem in bracket_embeds:
                            await ch_sum.send(embed=brem)
                except discord.HTTPException as e:
                    log.error("[SUMMARY] Bracket send failed for guild %s: %s", gid, e)
                except Exception as e:
                    log.error("[SUMMARY] Bracket fetch failed for guild %s: %s", gid, e)

        # ── 4. EOD leaderboard — REMOVED per spec ─────────────────────────────
        # Spec: "Do NOT include leaderboard updates in End-of-Day notifications."
        # Leaderboard updates are posted automatically via _update_leaderboard_in_predictions()
        # after every match full-time, pinned in the predictions channel.  No separate EOD post.


@daily_summary_loop.before_loop
async def _before_daily() -> None:
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════════

async def _startup_health_check() -> dict:
    """Run a lightweight health check on startup. Returns a status dict."""
    results: dict[str, str] = {}

    # API key
    key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    results["api_key"] = "✅ Set" if key else "❌ Missing — set FOOTBALL_DATA_API_KEY"

    # Discord token
    results["discord_token"] = "✅ Set" if os.environ.get("DISCORD_BOT_TOKEN") else "❌ Missing"

    # Match order loaded
    order = get_wc_match_order()
    results["match_order"] = f"✅ {len(order)} matches loaded" if order else "⚠️ No matches cached (will retry)"

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  ON_READY
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready() -> None:
    log.info("[BOT] Logged in as %s (ID: %s)", bot.user, bot.user.id)

    # Register persistent views
    bot.add_view(ModePicker())
    bot.add_view(InteractivePanel())
    bot.add_view(DashboardView())

    try:
        synced = await tree.sync()
        log.info("[BOT] Synced %d slash commands", len(synced))
    except Exception as e:
        log.error("[BOT] Slash sync failed: %s", e)

    await load_wc_match_order()

    # Startup health check
    health = await _startup_health_check()
    for check, result in health.items():
        if "❌" in result:
            log.error("[HEALTH] %s: %s", check, result)
        elif "⚠️" in result:
            log.warning("[HEALTH] %s: %s", check, result)
        else:
            log.info("[HEALTH] %s: %s", check, result)

    if not monitor_loop.is_running():
        monitor_loop.start()
    if not daily_summary_loop.is_running():
        daily_summary_loop.start()
    if not presence_loop.is_running():
        presence_loop.start()

    await _post_commands_menu_if_needed()

    # req #22: log timezone for each configured guild
    for gid, cfg in state.all_guild_configs().items():
        log.info("[BOT] Guild %s timezone: %s", gid, cfg.get("timezone", "UTC"))

    # req #20: permission audit — warn on any missing required permissions
    _REQUIRED_PERMS = {
        "send_messages":            "Send Messages",
        "embed_links":              "Embed Links",
        "manage_messages":          "Manage Messages (pin)",
        "create_public_threads":    "Create Public Threads",
        "send_messages_in_threads": "Send Messages in Threads",
    }
    for gid, cfg in state.all_guild_configs().items():
        guild_obj = bot.get_guild(int(gid))
        if not guild_obj:
            continue
        me = guild_obj.me
        if me is None:
            continue
        for ch_key in ("channel_id", "predictions_channel_id", "summary_channel_id"):
            cid = cfg.get(ch_key)
            if not cid:
                continue
            ch = bot.get_channel(int(cid))
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                continue
            perms = ch.permissions_for(me)
            for perm_attr, perm_name in _REQUIRED_PERMS.items():
                if not getattr(perms, perm_attr, True):
                    log.warning("[PERM] Guild %s channel %s (%s) missing: %s",
                                gid, cid, ch_key, perm_name)

    try:
        await broadcast(emb.embed_startup(bot.user))
    except Exception as e:
        log.error("[BOT] Startup broadcast failed: %s", e)

    log.info("[BOT] Ready — %d guilds configured", len(state.all_guild_configs()))


async def _post_commands_menu_if_needed() -> None:
    for gid, cfg in state.all_guild_configs().items():
        cid = cfg.get("commands_channel_id")
        if not cid:
            continue
        ch = bot.get_channel(int(cid))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            continue
        existing_id = state.get_commands_menu_message(gid)
        if existing_id:
            try:
                await ch.fetch_message(int(existing_id))
                continue
            except discord.HTTPException:
                pass
        try:
            msg = await ch.send(embed=emb.embed_commands_menu(), view=ModePicker())
            state.save_commands_menu_message(gid, msg.id)
        except discord.HTTPException as e:
            log.error("[MENU] Post failed for guild %s: %s", gid, e)


async def _post_or_update_interactive_panel(gid: str, cid: str) -> discord.Message | None:
    ch = await _resolve_channel(cid)
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return None

    panel_em = discord.Embed(
        title="⚽ World Cup 2026 — Self Service Panel",
        description=(
            "Click the buttons below to manage your personal preferences.\n"
            "**All responses are only visible to you.**\n\n"
            "🔔 **My Notifications** — toggle what you receive\n"
            "⭐ **My Teams** — manage followed national teams\n"
            "🏅 **Leaderboard** — current prediction standings\n"
            "📅 **Today's Matches** — see what's on today\n"
            "📊 **My Stats** — your prediction stats"
        ),
        color=emb.C_GOLD,
    )
    panel_em.set_thumbnail(url=emb.WC_ICON)
    panel_em.set_footer(text="🏆 FIFA World Cup 2026  •  Responses are private")

    old_id = state.get_interactive_panel_msg(gid, cid)
    if old_id:
        try:
            old = await ch.fetch_message(int(old_id))
            await old.edit(embed=panel_em, view=InteractivePanel())
            return old
        except discord.HTTPException:
            pass

    try:
        msg = await ch.send(embed=panel_em, view=InteractivePanel())
        state.save_interactive_panel_msg(gid, cid, msg.id)
        return msg
    except discord.HTTPException as e:
        log.error("[PANEL] Post failed for guild %s channel %s: %s", gid, cid, e)
        return None


@tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    """Surface and log slash-command failures.

    Without this, any exception inside a command shows the user a bare
    "This interaction failed" with nothing in the logs — which is exactly
    what made commands like /dashboard appear "broken".
    """
    original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    cmd_name = interaction.command.name if interaction.command else "?"

    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to use this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down — try again in {error.retry_after:.0f}s."
    else:
        msg = "Something went wrong running that command. Please try again in a moment."
        log.error("[CMD] /%s failed: %s", cmd_name, original, exc_info=original)

    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=_error_embed(msg), ephemeral=True)
        else:
            await interaction.response.send_message(embed=_error_embed(msg), ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=_error_embed(
            "You don't have permission to use this command.",
            "You need **Manage Channels** permission."
        ))
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.error("[BOT] Prefix command error: %s", error)


# ═══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

# ── /help ─────────────────────────────────────────────────────────────────────

_register("Match Commands", "help", "Browse all World Cup bot commands by category")

@tree.command(name="help", description="Browse all World Cup bot commands by category")
async def slash_help(interaction: discord.Interaction):
    view = HelpView(page=0)
    await interaction.response.send_message(
        embed=emb.embed_help(COMMAND_REGISTRY, 0), view=view, ephemeral=True
    )


# req #25: /dashboard removed — interactive panel is posted via /setup


# ── /setup ────────────────────────────────────────────────────────────────────

_register("Admin", "setup", "Run the interactive server setup wizard")

@tree.command(name="setup", description="Run the interactive setup wizard to configure channels, timezone & mode")
@app_commands.default_permissions(manage_channels=True)
async def slash_setup(interaction: discord.Interaction):
    view = SetupWizardView(step=0)
    await interaction.response.send_message(
        embed=emb.embed_setup_intro(), view=view, ephemeral=True
    )


# ── /today ────────────────────────────────────────────────────────────────────

_register("Match Commands", "today", "Show today's World Cup matches")

@tree.command(name="today", description="Show today's World Cup matches")
async def slash_today(interaction: discord.Interaction):
    await _safe_defer(interaction)
    try:
        matches = await get_todays_matches()
    except Exception as e:
        log.error("[CMD] /today error: %s", e)
        await interaction.followup.send(
            embed=_error_embed("Failed to load today's matches.", "Try again in a moment.")
        )
        return
    await _send_embeds(interaction, emb.embed_today(_annotate_matches(matches)))


@bot.command(name="today", help="Today's World Cup matches")
async def prefix_today(ctx):
    async with ctx.typing():
        matches = await get_todays_matches()
    for em_item in emb.embed_today(_annotate_matches(matches)):
        await ctx.send(embed=em_item)


# ── /live ─────────────────────────────────────────────────────────────────────

_register("Match Commands", "live", "Show currently live World Cup matches")

@tree.command(name="live", description="Show currently live World Cup matches")
async def slash_live(interaction: discord.Interaction):
    await _safe_defer(interaction)
    try:
        matches = await get_live_matches()
    except Exception as e:
        log.error("[CMD] /live error: %s", e)
        await interaction.followup.send(
            embed=_error_embed("Failed to load live matches.", "Try `/today` for today's schedule.")
        )
        return
    await interaction.followup.send(embed=emb.embed_live(_annotate_matches(matches)))


@bot.command(name="live", help="Currently live World Cup matches")
async def prefix_live(ctx):
    async with ctx.typing():
        matches = await get_live_matches()
    await ctx.send(embed=emb.embed_live(_annotate_matches(matches)))


# ── /nextmatch ────────────────────────────────────────────────────────────────

_register("Match Commands", "nextmatch", "Show the next upcoming World Cup match")

@tree.command(name="nextmatch", description="Show the next upcoming World Cup match")
async def slash_nextmatch(interaction: discord.Interaction):
    await _safe_defer(interaction)
    try:
        match = await get_next_match()
    except Exception as e:
        log.error("[CMD] /nextmatch error: %s", e)
        await interaction.followup.send(
            embed=_error_embed("Failed to load the next match.", "Try `/upcoming` to see all upcoming fixtures.")
        )
        return
    if not match:
        await interaction.followup.send(
            embed=discord.Embed(
                description="📭 No upcoming World Cup matches in the next 7 days.",
                color=emb.C_GREY,
            )
        )
        return
    await interaction.followup.send(embed=emb.embed_nextmatch(_annotate_match(match)))


@bot.command(name="nextmatch", help="Next World Cup match")
async def prefix_nextmatch(ctx):
    async with ctx.typing():
        match = await get_next_match()
    if not match:
        await ctx.send(embed=discord.Embed(description="📭 No upcoming matches.", color=emb.C_GREY))
        return
    await ctx.send(embed=emb.embed_nextmatch(_annotate_match(match)))


# ── /standings ────────────────────────────────────────────────────────────────

_register("Match Commands", "standings", "Show World Cup group standings")

@tree.command(name="standings", description="Show World Cup group standings")
async def slash_standings(interaction: discord.Interaction):
    await _safe_defer(interaction)
    try:
        data = await get_standings()
    except Exception as e:
        log.error("[CMD] /standings error: %s", e)
        await interaction.followup.send(
            embed=_error_embed(
                "Failed to load standings.",
                "Standings only appear during the group stage. Try `/upcoming` to see the schedule."
            )
        )
        return
    if not data:
        await interaction.followup.send(
            embed=_error_embed(
                "World Cup standings are not yet available.",
                "Standings appear once the group stage begins. Check `/upcoming` for fixture dates."
            )
        )
        return
    await _send_embeds(interaction, emb.embed_standings(data))


@bot.command(name="standings", help="World Cup standings")
async def prefix_standings(ctx):
    async with ctx.typing():
        data = await get_standings()
    if not data:
        await ctx.send(embed=_error_embed(
            "World Cup standings not yet available.",
            "Standings appear during the group stage."
        ))
        return
    for em_item in emb.embed_standings(data):
        await ctx.send(embed=em_item)


# ── /group ────────────────────────────────────────────────────────────────────

_register("Match Commands", "group", "Show a specific World Cup group table (A–P)")

@tree.command(name="group", description="Show a World Cup group table — e.g. /group A")
@app_commands.describe(letter="Group letter (A–P)")
@app_commands.autocomplete(letter=autocomplete_group)
async def slash_group(interaction: discord.Interaction, letter: str):
    await _safe_defer(interaction)
    target = letter.strip().upper()

    if target not in WC_GROUPS:
        await interaction.followup.send(
            embed=_error_embed(
                f"Group **{target}** is not a valid World Cup 2026 group.",
                "Valid groups are A through P. Try `/group A` or use autocomplete."
            )
        )
        return

    try:
        data = await get_standings()
    except Exception as e:
        log.error("[CMD] /group error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to fetch standings."))
        return

    if not data:
        await interaction.followup.send(
            embed=_error_embed(
                "Standings not yet available.",
                "Group tables appear once the group stage begins."
            )
        )
        return

    tables = data.get("standings", [])
    block  = next(
        (t for t in tables if (t.get("group") or "").upper().endswith(target)),
        None,
    )
    if not block:
        available = ", ".join(
            f"`{t.get('group','').replace('GROUP_','')}`"
            for t in tables if t.get("group")
        ) or "none yet"
        await interaction.followup.send(
            embed=_error_embed(
                f"Group **{target}** data not available yet.",
                f"Currently available: {available}"
            )
        )
        return

    await interaction.followup.send(embed=emb.embed_wc_group(target, block.get("table", [])))


@bot.command(name="group", help="World Cup group table. Usage: !group A")
async def prefix_group(ctx, letter: str = "A"):
    async with ctx.typing():
        data = await get_standings()
    if not data:
        await ctx.send(embed=_error_embed("Standings not yet available.", "Try again once the group stage begins."))
        return
    tables = data.get("standings", [])
    target = letter.strip().upper()
    block  = next((t for t in tables if (t.get("group") or "").upper().endswith(target)), None)
    if not block:
        await ctx.send(embed=_error_embed(f"Group {target} not found."))
        return
    await ctx.send(embed=emb.embed_wc_group(target, block.get("table", [])))


# ── /worldcup ─────────────────────────────────────────────────────────────────

_register("Match Commands", "worldcup", "Full World Cup 2026 overview — all groups")

@tree.command(name="worldcup", description="FIFA World Cup 2026 overview — all group tables")
async def slash_worldcup(interaction: discord.Interaction):
    await _safe_defer(interaction)
    try:
        data = await get_standings()
    except Exception as e:
        log.error("[CMD] /worldcup error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to fetch World Cup data."))
        return
    if not data:
        await interaction.followup.send(
            embed=_error_embed(
                "World Cup standings not yet available.",
                "They'll appear once the group stage begins. Check `/upcoming` for dates."
            )
        )
        return
    await _send_embeds(interaction, emb.embed_worldcup_overview(data))


@bot.command(name="worldcup", help="World Cup 2026 overview")
async def prefix_worldcup(ctx):
    async with ctx.typing():
        data = await get_standings()
    if not data:
        await ctx.send(embed=_error_embed("World Cup data not yet available."))
        return
    for em_item in emb.embed_worldcup_overview(data):
        await ctx.send(embed=em_item)


# ── /bracket ──────────────────────────────────────────────────────────────────

_register("Match Commands", "bracket", "World Cup 2026 knockout bracket")

@tree.command(name="bracket", description="FIFA World Cup 2026 knockout bracket")
async def slash_bracket(interaction: discord.Interaction):
    await _safe_defer(interaction)
    try:
        today   = datetime.now(timezone.utc)
        matches = await get_competition_matches(
            "2026-06-01",
            (today + timedelta(days=90)).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        log.error("[CMD] /bracket error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to load bracket data."))
        return
    knockout = [m for m in matches if m.get("stage") in
                ("LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")]
    await _send_embeds(interaction, emb.embed_bracket(_annotate_matches(knockout)))


@bot.command(name="bracket", help="World Cup knockout bracket")
async def prefix_bracket(ctx):
    today = datetime.now(timezone.utc)
    async with ctx.typing():
        matches = await get_competition_matches("2026-06-01", (today + timedelta(days=90)).strftime("%Y-%m-%d"))
    knockout = [m for m in matches if m.get("stage") in ("LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")]
    for em_item in emb.embed_bracket(_annotate_matches(knockout)):
        await ctx.send(embed=em_item)


# ── /upcoming ─────────────────────────────────────────────────────────────────

_register("Match Commands", "upcoming", "Upcoming World Cup fixtures")

@tree.command(name="upcoming", description="Upcoming World Cup fixtures")
@app_commands.describe(days="Days ahead to search (1–30, default 7)")
async def slash_upcoming(interaction: discord.Interaction, days: int = 7):
    await _safe_defer(interaction)
    days  = max(1, min(days, 30))
    today = datetime.now(timezone.utc)
    try:
        matches = await get_competition_matches(
            today.strftime("%Y-%m-%d"),
            (today + timedelta(days=days)).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        log.error("[CMD] /upcoming error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to load upcoming fixtures."))
        return
    await interaction.followup.send(embed=emb.embed_upcoming(_annotate_matches(matches), days))


@bot.command(name="upcoming", help="Upcoming matches. Usage: !upcoming 7")
async def prefix_upcoming(ctx, days: int = 7):
    days  = max(1, min(days, 30))
    today = datetime.now(timezone.utc)
    async with ctx.typing():
        matches = await get_competition_matches(
            today.strftime("%Y-%m-%d"),
            (today + timedelta(days=days)).strftime("%Y-%m-%d"),
        )
    await ctx.send(embed=emb.embed_upcoming(_annotate_matches(matches), days))


# req #25: /schedule removed — use /upcoming instead

# ── /match ────────────────────────────────────────────────────────────────────

_register("Match Commands", "match", "Show details for a specific match — use autocomplete to pick one")

@tree.command(name="match", description="Show full details for a specific match by ID")
@app_commands.describe(match_id="Pick a match from the list or type a match ID")
@app_commands.autocomplete(match_id=autocomplete_match_id)
async def slash_match(interaction: discord.Interaction, match_id: int):
    await _safe_defer(interaction)
    try:
        detail = await get_match_detail(match_id)
    except Exception as e:
        log.error("[CMD] /match error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to load match data."))
        return
    if not detail:
        await interaction.followup.send(
            embed=_error_embed(
                f"Match `{match_id}` not found.",
                "Double-check the ID. You can find IDs using `/today` or `/upcoming`."
            )
        )
        return
    status = detail.get("status", "")
    ann    = _annotate_match(detail)  # injects matchNumber/matchDay → shown by embeds
    if status == "FINISHED":
        await interaction.followup.send(embed=emb.embed_fulltime(ann, detail))
    elif status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
        await interaction.followup.send(embed=emb.embed_live([ann]))
    else:
        await interaction.followup.send(embed=emb.embed_nextmatch(ann))


# ── /team ─────────────────────────────────────────────────────────────────────

_register("Match Commands", "team", "Show a national team's profile and fixtures — autocomplete with flags")

@tree.command(name="team", description="Show a national team's profile and World Cup fixtures")
@app_commands.describe(name="National team name e.g. Brazil, Germany")
@app_commands.autocomplete(name=autocomplete_team)
async def slash_team(interaction: discord.Interaction, name: str):
    await _safe_defer(interaction)
    try:
        team = await search_team(name)
    except Exception as e:
        log.error("[CMD] /team error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to search for that team."))
        return
    if not team:
        await interaction.followup.send(
            embed=_error_embed(
                f"National team `{name}` not found.",
                "Try a different spelling — e.g. `Brazil`, `United States`, `Türkiye`. Use autocomplete for suggestions."
            )
        )
        return
    tid = team["id"]
    try:
        recent, upcoming_m = await asyncio.gather(
            get_team_matches(tid, status="FINISHED", limit=5),
            get_team_matches(tid, status="SCHEDULED", limit=5),
        )
    except Exception as e:
        log.error("[CMD] /team match fetch error: %s", e)
        recent, upcoming_m = [], []
    await interaction.followup.send(embed=emb.embed_team(
        team,
        _annotate_matches(recent),
        _annotate_matches(upcoming_m),
    ))


@bot.command(name="team", help="Team profile. Usage: !team Brazil")
async def prefix_team(ctx, *, name: str):
    async with ctx.typing():
        team = await search_team(name)
    if not team:
        await ctx.send(embed=_error_embed(f"Team `{name}` not found.", "Try a different spelling."))
        return
    tid = team["id"]
    recent, upcoming_m = await asyncio.gather(
        get_team_matches(tid, status="FINISHED", limit=5),
        get_team_matches(tid, status="SCHEDULED", limit=5),
    )
    await ctx.send(embed=emb.embed_team(team, _annotate_matches(recent), _annotate_matches(upcoming_m)))


# ── /matchcenter ──────────────────────────────────────────────────────────────

_register("Match Commands", "matchcenter", "Live matches, next match, standings & leaderboard — all at once")

@tree.command(
    name="matchcenter",
    description="Live matches, next match, standings snapshot & prediction leaderboard"
)
async def slash_matchcenter(interaction: discord.Interaction):
    await _safe_defer(interaction)
    gid = str(interaction.guild_id) if interaction.guild else None

    try:
        live, next_m, standings_data = await asyncio.gather(
            get_live_matches(),
            get_next_match(),
            get_standings(),
        )
    except Exception as e:
        log.error("[CMD] /matchcenter error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to load Match Center data.", "Try again in a moment."))
        return

    standings_snap = standings_data.get("standings", []) if standings_data else []
    lb = state.get_leaderboard(gid) if gid else {}
    embeds = emb.embed_matchcenter(
        _annotate_matches(live),
        _annotate_match(next_m) if next_m else None,
        standings_snap, lb, interaction.guild
    )
    await _send_embeds(interaction, embeds)


@bot.command(name="matchcenter", help="Live matches + next match + standings + leaderboard")
async def prefix_matchcenter(ctx):
    async with ctx.typing():
        live, next_m, standings_data = await asyncio.gather(
            get_live_matches(), get_next_match(), get_standings()
        )
    standings_snap = standings_data.get("standings", []) if standings_data else []
    lb     = state.get_leaderboard(str(ctx.guild.id)) if ctx.guild else {}
    embeds = emb.embed_matchcenter(
        _annotate_matches(live),
        _annotate_match(next_m) if next_m else None,
        standings_snap, lb, ctx.guild
    )
    for em_item in embeds:
        await ctx.send(embed=em_item)


# ── /scorers ──────────────────────────────────────────────────────────────────

_register("Match Commands", "scorers", "Top scorers in the World Cup")

@tree.command(name="scorers", description="Top scorers in the FIFA World Cup 2026")
@app_commands.describe(limit="How many scorers to show (1–20, default 10)")
async def slash_scorers(interaction: discord.Interaction, limit: int = 10):
    await _safe_defer(interaction)
    limit = max(1, min(limit, 20))
    try:
        scorers = await get_scorers(limit)
    except Exception as e:
        log.error("[CMD] /scorers error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to load scorers."))
        return
    if not scorers:
        await interaction.followup.send(
            embed=discord.Embed(
                description="📭 No scorer data available yet — the tournament hasn't started.",
                color=emb.C_GREY,
            )
        )
        return
    em = discord.Embed(title="⚽ Top Scorers — FIFA World Cup 2026", color=emb.C_GOLD)
    lines = []
    for rank, entry in enumerate(scorers, 1):
        player = entry.get("player", {})
        team   = entry.get("team", {})
        goals  = entry.get("goals", 0)
        name   = player.get("name", "?")
        flag   = team_flag(team)
        lines.append(f"`{rank:>2}.` {flag} **{name}** — {goals} goal{'s' if goals != 1 else ''}")
    em.description = "\n".join(lines)
    em.set_footer(text="🏆 FIFA World Cup 2026")
    await interaction.followup.send(embed=em)


# ── /predict ──────────────────────────────────────────────────────────────────

_register("Predictions", "predict", "Predict the score — pick a match from autocomplete, flags shown in UI")

@tree.command(name="predict", description="Make a score prediction for an upcoming World Cup match")
@app_commands.describe(match_id="Pick a match from the list or type a match ID")
@app_commands.autocomplete(match_id=autocomplete_match_id)
async def slash_predict(interaction: discord.Interaction, match_id: int):
    await _safe_defer(interaction)
    try:
        detail = await get_match_detail(match_id)
    except Exception as e:
        log.error("[CMD] /predict error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to load match."))
        return
    if not detail:
        await interaction.followup.send(
            embed=_error_embed(f"Match `{match_id}` not found.", "Find IDs using `/today` or `/upcoming`.")
        )
        return
    status = detail.get("status", "")
    if status == "FINISHED":
        await interaction.followup.send(
            embed=_error_embed("This match has already finished.", "Use `/upcoming` to find an upcoming match to predict.")
        )
        return
    if status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
        await interaction.followup.send(
            embed=_error_embed("This match is already live — predictions are locked.", "Look out for the next prediction poll!")
        )
        return

    home = team_display(detail.get("homeTeam", {}))
    away = team_display(detail.get("awayTeam", {}))
    ko   = is_knockout(detail)
    mid  = str(match_id)
    num  = _match_num_str(match_id)

    existing = state.get_score_predictions(mid).get(str(interaction.user.id))
    view = ScoreInputView(mid, home, away, ko)
    header = f"**Match {num}** — " if num else ""
    if existing:
        view.home_score = existing["home"]
        view.away_score = existing["away"]
        view.changed    = True
        view._sync()
        content = f"{header}**Your current prediction:** {existing['home']}–{existing['away']}\nUpdate it below."
    else:
        content = f"{header}{view._content()}"

    await interaction.followup.send(content=content, view=view, ephemeral=True)


# ── /leaderboard ──────────────────────────────────────────────────────────────

_register("Predictions", "leaderboard", "View the all-time prediction leaderboard")

@tree.command(name="leaderboard", description="View the World Cup prediction leaderboard")
async def slash_leaderboard(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("This command must be used inside a server."),
            ephemeral=True,
        )
        return
    gid = str(interaction.guild_id)
    lb  = state.get_leaderboard(gid)
    await interaction.response.send_message(embed=emb.embed_leaderboard(interaction.guild, lb))


@bot.command(name="leaderboard", help="Prediction leaderboard")
async def prefix_leaderboard(ctx):
    lb = state.get_leaderboard(str(ctx.guild.id))
    await ctx.send(embed=emb.embed_leaderboard(ctx.guild, lb))


# ── /monthlyleaderboard ───────────────────────────────────────────────────────

_register("Predictions", "monthlyleaderboard", "View this month's prediction leaderboard")

@tree.command(name="monthlyleaderboard", description="View the monthly prediction leaderboard")
@app_commands.describe(month="Month in YYYY-MM format (leave blank for current month)")
async def slash_monthly_lb(interaction: discord.Interaction, month: str = ""):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("Use this inside a server."), ephemeral=True
        )
        return
    gid = str(interaction.guild_id)
    now = datetime.now(timezone.utc)

    if month:
        try:
            datetime.strptime(month, "%Y-%m")
            month_key = month
        except ValueError:
            await interaction.response.send_message(
                embed=_error_embed(
                    f"Invalid month format `{month}`.",
                    "Use YYYY-MM format — e.g. `2026-07`"
                ),
                ephemeral=True,
            )
            return
    else:
        month_key = now.strftime("%Y-%m")

    data        = state.get_monthly_leaderboard(gid, month_key)
    month_label = datetime.strptime(month_key, "%Y-%m").strftime("%B %Y")
    await interaction.response.send_message(
        embed=emb.embed_monthly_leaderboard(interaction.guild, data, month_label)
    )


# ── /predstats ────────────────────────────────────────────────────────────────

_register("Predictions", "predstats", "View your advanced prediction statistics")

@tree.command(name="predstats", description="View your advanced prediction statistics — streak, exact scores, accuracy")
@app_commands.describe(user="View another user's stats (leave blank for yours)")
async def slash_predstats(interaction: discord.Interaction, user: discord.Member | None = None):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("Use this inside a server."), ephemeral=True
        )
        return
    target = user or interaction.user
    uid    = str(target.id)
    gid    = str(interaction.guild_id)
    stats  = state.get_prediction_stats(uid, gid)
    lb     = state.get_leaderboard(gid)
    sorted_lb = sorted(lb.items(), key=lambda x: x[1].get("points", 0), reverse=True)
    rank   = next((r + 1 for r, (u, _) in enumerate(sorted_lb) if u == uid), None)
    await interaction.response.send_message(embed=emb.embed_prediction_stats(target, stats, rank))


# ── /followteam ───────────────────────────────────────────────────────────────

_register("Following", "followteam", "Follow a national team — autocomplete shows flags for all 48 nations")

@tree.command(name="followteam", description="Follow a national team to track their World Cup journey")
@app_commands.describe(name="National team name e.g. Brazil, England")
@app_commands.autocomplete(name=autocomplete_team)
async def slash_followteam(interaction: discord.Interaction, name: str):
    uid       = str(interaction.user.id)
    following = dict(state.get_user_following(uid))
    teams     = list(following.get("teams", []))

    if len(teams) >= 20:
        await interaction.response.send_message(
            embed=_error_embed(
                "You're already following 20 teams (the maximum).",
                "Use `/myteams` to unfollow some teams first."
            ),
            ephemeral=True,
        )
        return

    name_normalised = name.strip().title()
    if name_normalised in teams:
        await interaction.response.send_message(
            embed=_error_embed(
                f"You're already following **{name_normalised}**.",
                "Use `/myteams` to manage your followed teams."
            ),
            ephemeral=True,
        )
        return

    teams.append(name_normalised)
    following["teams"] = teams
    state.set_user_following(uid, following)
    flag = team_flag({"name": name_normalised})
    await interaction.response.send_message(
        embed=_success_embed(f"Now following {flag} **{name_normalised}**!\nYou'll be notified when they play."),
        ephemeral=True,
    )


@bot.command(name="followteam", help="Follow a national team. Usage: !followteam Brazil")
async def prefix_followteam(ctx, *, name: str):
    uid       = str(ctx.author.id)
    following = dict(state.get_user_following(uid))
    teams     = list(following.get("teams", []))
    name_norm = name.strip().title()
    if name_norm in teams:
        await ctx.send(embed=_error_embed(f"You're already following **{name_norm}**."))
        return
    teams.append(name_norm)
    following["teams"] = teams
    state.set_user_following(uid, following)
    flag = team_flag({"name": name_norm})
    await ctx.send(embed=_success_embed(f"Now following {flag} **{name_norm}**!"))


# ── /unfollowteam ─────────────────────────────────────────────────────────────

_register("Following", "unfollowteam", "Unfollow a national team — autocomplete shows your followed teams")

@tree.command(name="unfollowteam", description="Unfollow a national team")
@app_commands.describe(name="Team name to unfollow")
@app_commands.autocomplete(name=autocomplete_followed_team)
async def slash_unfollowteam(interaction: discord.Interaction, name: str):
    uid       = str(interaction.user.id)
    following = dict(state.get_user_following(uid))
    teams     = [t for t in following.get("teams", []) if t.lower() != name.strip().lower()]

    if len(teams) == len(following.get("teams", [])):
        await interaction.response.send_message(
            embed=_error_embed(
                f"You're not following **{name}**.",
                "Use `/myteams` to see who you're following."
            ),
            ephemeral=True,
        )
        return

    following["teams"] = teams
    state.set_user_following(uid, following)
    await interaction.response.send_message(
        embed=_success_embed(f"Unfollowed **{name}**."),
        ephemeral=True,
    )


@bot.command(name="unfollowteam", help="Unfollow a team. Usage: !unfollowteam Brazil")
async def prefix_unfollowteam(ctx, *, name: str):
    uid       = str(ctx.author.id)
    following = dict(state.get_user_following(uid))
    following["teams"] = [t for t in following.get("teams", []) if t.lower() != name.strip().lower()]
    state.set_user_following(uid, following)
    await ctx.send(embed=_success_embed(f"Unfollowed **{name}**."))


# ── /myteams ──────────────────────────────────────────────────────────────────

_register("Following", "myteams", "View and manage your followed national teams")

@tree.command(name="myteams", description="View and manage your followed national teams")
async def slash_myteams(interaction: discord.Interaction):
    uid  = str(interaction.user.id)
    em   = _build_teams_embed(uid)
    view = UnfollowTeamView(uid) if state.get_user_following(uid).get("teams") else None
    await interaction.response.send_message(embed=em, view=view, ephemeral=True)


@bot.command(name="myteams", help="View your followed teams")
async def prefix_myteams(ctx):
    uid = str(ctx.author.id)
    await ctx.send(embed=_build_teams_embed(uid))


# ── /mysettings ───────────────────────────────────────────────────────────────

_register("Settings", "mysettings", "Toggle your personal notification settings")

@tree.command(name="mysettings", description="Toggle your personal World Cup notification settings")
async def slash_mysettings(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("Use this command inside a server."), ephemeral=True
        )
        return
    uid  = str(interaction.user.id)
    gid  = str(interaction.guild_id)
    view = UserSettingsView(uid, gid)
    await interaction.response.send_message(
        embed=_build_settings_embed(uid, gid), view=view, ephemeral=True
    )


# ── /viewsettings ─────────────────────────────────────────────────────────────

_register("Settings", "viewsettings", "View your current notification settings (read-only)")

@tree.command(name="viewsettings", description="View your current notification settings")
async def slash_viewsettings(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("Use this inside a server."), ephemeral=True
        )
        return
    uid = str(interaction.user.id)
    gid = str(interaction.guild_id)
    await interaction.response.send_message(
        embed=_build_settings_embed(uid, gid), ephemeral=True
    )


# ── /resetmysettings ──────────────────────────────────────────────────────────

_register("Settings", "resetmysettings", "Reset all your personal notification settings to server defaults")

@tree.command(name="resetmysettings", description="Reset all personal notification settings to server defaults")
async def slash_resetmysettings(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("Use this inside a server."), ephemeral=True
        )
        return
    uid = str(interaction.user.id)
    gid = str(interaction.guild_id)
    state.set_user_prefs(uid, gid, {})
    await interaction.response.send_message(
        embed=_success_embed("Your notification settings have been reset to server defaults."),
        ephemeral=True,
    )


# ── /mytimezone ───────────────────────────────────────────────────────────────

_register("Settings", "mytimezone", "Set your personal timezone for match times")

@tree.command(name="mytimezone", description="Set your personal timezone — match times will show in your local time")
@app_commands.describe(timezone="Start typing to search — e.g. 'new york', 'london', 'tokyo', 'UTC'")
@app_commands.autocomplete(timezone=autocomplete_timezone)
async def slash_mytimezone(interaction: discord.Interaction, timezone: str = ""):
    uid = str(interaction.user.id)
    gid = str(interaction.guild_id) if interaction.guild else "dm"

    if timezone:
        try:
            tz    = ZoneInfo(timezone)
            local = datetime.now(tz).strftime("%A, %d %b %Y  •  %H:%M %Z")
            following = dict(state.get_user_following(uid))
            following["timezone"] = timezone
            state.set_user_following(uid, following)
            log.info("[SETTINGS] User %s set timezone to %s (autocomplete)", uid, timezone)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🕐 Timezone Updated",
                    description=(
                        f"✅ Set to **{timezone}**\n"
                        f"🕐 Your local time right now: **{local}**\n\n"
                        "All match times shown to you will now use this timezone.\n"
                        "You can update it anytime with `/mytimezone` or via `/mysettings`."
                    ),
                    color=emb.C_GREEN,
                ).set_footer(text="🏆 FIFA World Cup 2026  •  Only visible to you"),
                ephemeral=True,
            )
        except (ZoneInfoNotFoundError, KeyError):
            await interaction.response.send_message(
                embed=_error_embed(
                    f"`{timezone}` is not a valid timezone.",
                    "Use the autocomplete suggestions — start typing a city or region name."
                ),
                ephemeral=True,
            )
        return

    view = UserTimezoneStandaloneView(uid, gid)
    await interaction.response.send_message(
        embed=_build_mytimezone_embed(uid),
        view=view,
        ephemeral=True,
    )


def _build_mytimezone_embed(uid: str) -> discord.Embed:
    tz_name = _user_tz_str(uid)
    try:
        tz    = ZoneInfo(tz_name)
        local = datetime.now(tz).strftime("%A, %d %b %Y  •  %H:%M %Z")
    except Exception:
        local = "unknown"

    em = discord.Embed(
        title="🕐 My Timezone",
        description=(
            f"**Current timezone:** `{tz_name}`\n"
            f"**Your local time right now:** {local}\n\n"
            "Pick your timezone from the dropdown below.\n"
            "Match kick-off times shown to you in commands like `/today`, `/upcoming`, and `/nextmatch` "
            "will use this timezone."
        ),
        color=emb.C_BLUE,
    )
    em.set_footer(text="🏆 FIFA World Cup 2026  •  Only visible to you")
    return em


class UserTimezoneStandaloneView(discord.ui.View):
    """Standalone timezone picker (used by /mytimezone command)."""

    def __init__(self, uid: str, gid: str):
        super().__init__(timeout=300)
        self.uid = uid
        self.gid = gid

        current = _user_tz_str(uid)
        options = [
            discord.SelectOption(
                label=opt.label, value=opt.value, emoji=opt.emoji,
                default=(opt.value == current),
            )
            for opt in _USER_TZ_OPTIONS
        ]
        select = discord.ui.Select(
            placeholder=f"🕐 Current: {current} — pick a new timezone…",
            min_values=1, max_values=1,
            options=options, row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, i: discord.Interaction) -> None:
        tz_name  = i.data["values"][0]
        following = dict(state.get_user_following(self.uid))
        following["timezone"] = tz_name
        state.set_user_following(self.uid, following)

        try:
            tz    = ZoneInfo(tz_name)
            local = datetime.now(tz).strftime("%A, %d %b %Y  •  %H:%M %Z")
        except Exception:
            local = "unknown"

        new_view = UserTimezoneStandaloneView(self.uid, self.gid)
        await i.response.edit_message(
            embed=discord.Embed(
                title="🕐 My Timezone",
                description=(
                    f"✅ Timezone updated to **{tz_name}**!\n"
                    f"**Your local time right now:** {local}\n\n"
                    "All match times shown to you will now use this timezone.\n"
                    "You can change it again at any time using `/mytimezone` or via `/mysettings`."
                ),
                color=emb.C_GREEN,
            ).set_footer(text="🏆 FIFA World Cup 2026  •  Only visible to you"),
            view=new_view,
        )
        log.info("[SETTINGS] User %s set timezone to %s", self.uid, tz_name)

    @discord.ui.button(label="🔄 Reset to UTC", style=discord.ButtonStyle.secondary, row=1)
    async def reset_btn(self, i: discord.Interaction, b: discord.ui.Button):
        following = dict(state.get_user_following(self.uid))
        following["timezone"] = "UTC"
        state.set_user_following(self.uid, following)
        new_view = UserTimezoneStandaloneView(self.uid, self.gid)
        await i.response.edit_message(
            embed=_build_mytimezone_embed(self.uid), view=new_view
        )


# ── /timezone ─────────────────────────────────────────────────────────────────

_register("Settings", "timezone", "Show this server's current timezone")

@tree.command(name="timezone", description="Show this server's timezone — use /mytimezone to set your own")
async def slash_timezone(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("Use this inside a server."), ephemeral=True
        )
        return
    uid    = str(interaction.user.id)
    cfg    = state.get_guild_config(str(interaction.guild_id))
    tz_str = cfg.get("timezone", "UTC")
    tz     = _tz_for(interaction.guild_id)
    local  = datetime.now(tz).strftime("%H:%M")

    user_tz    = _user_tz_str(uid)
    user_local = datetime.now(_user_tz_for(uid)).strftime("%H:%M")

    em = discord.Embed(color=emb.C_BLUE)
    em.add_field(
        name="🏟️ Server Timezone",
        value=(
            f"**{tz_str}**\n"
            f"Current local time: **{local}**\n"
            "Daily summary sends at 23:50 in this timezone."
        ),
        inline=False,
    )
    em.add_field(
        name="🙋 Your Personal Timezone",
        value=(
            f"**{user_tz}**\n"
            f"Your local time: **{user_local}**\n"
            "Use `/mytimezone` to change it."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=em)


# ── /status ───────────────────────────────────────────────────────────────────

_register("Admin", "status", "Show the bot's configuration for this server")

@tree.command(name="status", description="Show the bot's configuration for this server")
async def slash_status(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("Use this inside a server."), ephemeral=True
        )
        return
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    await interaction.response.send_message(
        embed=emb.embed_status(interaction.guild, cfg, monitor_loop.is_running())
    )


@bot.command(name="status", help="Bot configuration status")
async def prefix_status(ctx):
    cfg = state.get_guild_config(str(ctx.guild.id))
    await ctx.send(embed=emb.embed_status(ctx.guild, cfg, monitor_loop.is_running()))


# ── Admin channel setup commands ───────────────────────────────────────────────

def _make_channel_set_embed(channel: discord.TextChannel | discord.Thread, purpose: str) -> discord.Embed:
    return _success_embed(f"**#{channel.name}** is now the **{purpose}** channel.")


@tree.command(name="setmatcheschannel", description="Set this channel for live match notifications")
@app_commands.default_permissions(manage_channels=True)
async def slash_setmatcheschannel(interaction: discord.Interaction):
    state.set_channel_id(str(interaction.guild_id), "channel_id", str(interaction.channel_id))
    log.info("[ADMIN] Live channel set for guild %s → %s", interaction.guild_id, interaction.channel_id)
    await interaction.response.send_message(embed=_success_embed(
        f"**#{interaction.channel.name}** is now the live match alerts channel.\n"
        "Use `/setmode` to choose notification verbosity."
    ))

_register("Admin", "setmatcheschannel", "Set the live match notifications channel")


@tree.command(name="setpredictionschannel", description="Set this channel for prediction polls and MOTM voting")
@app_commands.default_permissions(manage_channels=True)
async def slash_setpredictionschannel(interaction: discord.Interaction):
    state.set_channel_id(str(interaction.guild_id), "predictions_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(
        embed=_make_channel_set_embed(interaction.channel, "prediction polls & MOTM voting")
    )

_register("Admin", "setpredictionschannel", "Set the predictions & MOTM channel")


# req #25: /setresultschannel removed — use /setpredictionschannel


@tree.command(name="setsummarychannel", description="Set this channel for match recaps and highlights")
@app_commands.default_permissions(manage_channels=True)
async def slash_setsummarychannel(interaction: discord.Interaction):
    state.set_channel_id(str(interaction.guild_id), "summary_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(
        embed=_make_channel_set_embed(interaction.channel, "match recaps & highlights")
    )

_register("Admin", "setsummarychannel", "Set the match recaps & highlights channel")


@tree.command(name="setcommandschannel", description="Set this channel for the notification mode picker")
@app_commands.default_permissions(manage_channels=True)
async def slash_setcommandschannel(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)
    state.set_channel_id(gid, "commands_channel_id", cid)
    state.set_channel_id(gid, "interactive_channel_id", cid)
    await interaction.response.send_message(
        embed=_make_channel_set_embed(interaction.channel, "notification mode picker")
    )
    await _post_commands_menu_if_needed()
    await _post_or_update_interactive_panel(gid, cid)

_register("Admin", "setcommandschannel", "Set the notification mode picker channel")


@tree.command(name="setinteractivechannel", description="Post the interactive self-service panel to this channel")
@app_commands.default_permissions(manage_channels=True)
async def slash_setinteractivechannel(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)

    old_id = state.get_interactive_panel_msg(gid, cid)
    if old_id:
        ch = bot.get_channel(int(cid))
        if ch:
            try:
                old = await ch.fetch_message(int(old_id))
                await old.delete()
            except discord.HTTPException:
                pass

    panel_em = discord.Embed(
        title="⚽ World Cup 2026 — Self Service Panel",
        description=(
            "Click the buttons below to manage your personal preferences.\n"
            "**All responses are only visible to you.**\n\n"
            "🔔 **My Notifications** — toggle what you receive\n"
            "⭐ **My Teams** — manage followed national teams\n"
            "🏅 **Leaderboard** — current prediction standings\n"
            "📅 **Today's Matches** — see what's on today\n"
            "📊 **My Stats** — your prediction stats"
        ),
        color=emb.C_GOLD,
    )
    panel_em.set_thumbnail(url=emb.WC_ICON)
    panel_em.set_footer(text="🏆 FIFA World Cup 2026  •  Responses are private")

    try:
        msg = await interaction.channel.send(embed=panel_em, view=InteractivePanel())
        state.save_interactive_panel_msg(gid, cid, msg.id)
        await interaction.response.send_message(
            embed=_success_embed(f"Interactive panel posted in {interaction.channel.mention}."),
            ephemeral=True,
        )
    except discord.HTTPException as e:
        await interaction.response.send_message(
            embed=_error_embed(
                f"Failed to post panel: {e}",
                "Make sure I have Send Messages & Embed Links permissions here."
            ),
            ephemeral=True,
        )

_register("Admin", "setinteractivechannel", "Post the user self-service panel")


# req #25: /setmode removed — mode selection is now part of /setup wizard


@tree.command(name="settimezone", description="Set the server timezone for daily summaries")
@app_commands.describe(timezone_name="Start typing a city or region — e.g. 'new york', 'london', 'tokyo'")
@app_commands.autocomplete(timezone_name=autocomplete_timezone)
@app_commands.default_permissions(manage_channels=True)
async def slash_settimezone(interaction: discord.Interaction, timezone_name: str):
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, KeyError):
        await interaction.response.send_message(
            embed=_error_embed(
                f"`{timezone_name}` is not a valid IANA timezone.",
                "Examples: `America/New_York` · `Europe/London` · `America/Sao_Paulo` · `UTC`\n"
                "Or use `/setup` to pick from a list."
            ),
            ephemeral=True,
        )
        return
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    cfg["timezone"] = timezone_name
    state.set_guild_config(gid, cfg)
    now_local = datetime.now(tz).strftime("%H:%M")
    await interaction.response.send_message(
        embed=_success_embed(
            f"Timezone set to **{timezone_name}**  (current local time: {now_local})\n"
            "Daily summaries will send at 23:50 in this timezone."
        )
    )

_register("Admin", "settimezone", "Set the server timezone for daily summaries")


@tree.command(name="resetleaderboard", description="Reset the prediction leaderboard (admin only)")
@app_commands.default_permissions(administrator=True)
async def slash_resetleaderboard(interaction: discord.Interaction):
    state.reset_leaderboard(str(interaction.guild_id))
    log.info("[ADMIN] Leaderboard reset for guild %s by %s", interaction.guild_id, interaction.user)
    await interaction.response.send_message(
        embed=_success_embed("Prediction leaderboard has been reset.")
    )

_register("Admin", "resetleaderboard", "Reset the prediction leaderboard")


# req #25: /updatecommandsmenu removed — commands menu is auto-managed


# ═══════════════════════════════════════════════════════════════════════════════
#  DIAGNOSTICS COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

# ── /checkapi ─────────────────────────────────────────────────────────────────

_register("Admin", "checkapi", "Diagnose the football-data.org API connection")

@tree.command(name="checkapi", description="[Admin] Check API key, tier, and whether WC 2026 data is available")
@app_commands.default_permissions(administrator=True)
async def slash_checkapi(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    import football_api as _fa

    key = _fa._current_api_key()
    em  = discord.Embed(title="🔧 API Diagnostic", color=emb.C_BLUE)

    if not key:
        em.color = emb.C_RED
        em.add_field(
            name="❌ API Key",
            value=(
                "**`FOOTBALL_DATA_API_KEY` is not set.**\n"
                "Add it to your environment variables, then restart the bot.\n"
                "Get a free key at <https://www.football-data.org/client/register>"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=em, ephemeral=True)
        return

    em.add_field(name="✅ API Key", value=f"Set (`…{key[-4:]}`)", inline=True)

    raw = await _fa._get("/competitions/WC")
    if raw is None:
        em.color = emb.C_RED
        em.add_field(
            name="❌ WC Competition",
            value=(
                "API returned no data for `WC`.\n"
                "Possible causes:\n"
                "• Invalid API key (check for typos)\n"
                "• Your plan doesn't include WC 2026 — "
                "[upgrade at football-data.org](https://www.football-data.org/coverage)\n"
                "• The competition hasn't been published yet"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=em, ephemeral=True)
        return

    comp_name   = raw.get("name", "?")
    comp_season = (raw.get("currentSeason") or {})
    season_year = comp_season.get("startDate", "?")[:4]
    em.add_field(name="✅ WC Competition", value=f"{comp_name} ({season_year})", inline=True)

    today_matches = await _fa.get_todays_matches()
    em.add_field(
        name="📅 Today's Matches",
        value=str(len(today_matches)) + (" match(es) found" if today_matches else " — none scheduled today"),
        inline=True,
    )

    live_matches = await _fa.get_live_matches()
    em.add_field(
        name="⚽ Live Right Now",
        value=str(len(live_matches)) + (" live" if live_matches else " — none live"),
        inline=True,
    )

    standings = await _fa.get_standings()
    em.add_field(
        name="🏆 Standings",
        value="Available" if standings else "Not yet available (group stage not started?)",
        inline=True,
    )

    order = get_wc_match_order()
    em.add_field(
        name="🔢 Match Order Cache",
        value=f"{len(order)} matches cached" if order else "❌ Empty — restart the bot",
        inline=True,
    )

    em.add_field(
        name="ℹ️ Tier note",
        value=(
            "football-data.org free tier includes WC data during the tournament.\n"
            "If you see auth errors, verify your key or upgrade your plan."
        ),
        inline=False,
    )

    em.color = emb.C_GREEN
    em.set_footer(text="🏆 FIFA World Cup 2026  •  /checkapi")
    await interaction.followup.send(embed=em, ephemeral=True)


# ── /health ───────────────────────────────────────────────────────────────────

_register("Admin", "health", "Runtime health check — bot tasks, API, config")

@tree.command(name="health", description="[Admin] Runtime health check — bot tasks, API connectivity, config")
@app_commands.default_permissions(administrator=True)
async def slash_health(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    em = discord.Embed(title="💚 Runtime Health Check", color=emb.C_GREEN)

    # Bot state
    em.add_field(name="🤖 Bot", value=f"Online as `{bot.user}`", inline=True)
    em.add_field(name="🌐 Guilds", value=str(len(bot.guilds)), inline=True)
    em.add_field(
        name="⚙️ Configured Guilds",
        value=str(len(state.all_guild_configs())),
        inline=True,
    )

    # Background tasks
    monitor_ok   = "✅ Running" if monitor_loop.is_running() else "❌ Stopped — restart bot"
    summary_ok   = "✅ Running" if daily_summary_loop.is_running() else "❌ Stopped"
    em.add_field(name="🔄 Monitor Loop", value=monitor_ok, inline=True)
    em.add_field(name="📅 Daily Summary Loop", value=summary_ok, inline=True)

    # Match order cache
    order = get_wc_match_order()
    em.add_field(
        name="🔢 Match Cache",
        value=f"✅ {len(order)} WC matches" if order else "⚠️ Empty",
        inline=True,
    )

    # API key
    key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    em.add_field(
        name="🔑 API Key",
        value=f"✅ Set (`…{key[-4:]}`)" if key else "❌ Missing",
        inline=True,
    )

    # Latency
    em.add_field(name="📡 Discord Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)

    # Guild config summary
    if interaction.guild:
        gid = str(interaction.guild_id)
        cfg = state.get_guild_config(gid)
        channels = {
            "Live Alerts":         cfg.get("channel_id"),
            "Predictions+Results": cfg.get("predictions_channel_id"),
            "Recaps":              cfg.get("summary_channel_id"),
        }
        ch_lines = "\n".join(
            f"{'✅' if cid else '❌'}  {name}: {'<#' + cid + '>' if cid else 'not set'}"
            for name, cid in channels.items()
        )
        em.add_field(name="📋 This Server's Channels", value=ch_lines, inline=False)

    em.set_footer(text=f"🏆 FIFA World Cup 2026  •  {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    await interaction.followup.send(embed=em, ephemeral=True)


# ── /diagnostics ─────────────────────────────────────────────────────────────

_register("Admin", "diagnostics", "Full system diagnostic — API, config, state, tasks")

@tree.command(name="diagnostics", description="[Admin] Full system diagnostic — all subsystems checked")
@app_commands.default_permissions(administrator=True)
async def slash_diagnostics(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    results: list[tuple[str, str, str]] = []  # (system, status_emoji, detail)

    # 1 — Environment variables
    discord_tok = "✅" if os.environ.get("DISCORD_BOT_TOKEN") else "❌"
    api_key     = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    results.append(("DISCORD_BOT_TOKEN", discord_tok, "Set" if discord_tok == "✅" else "**MISSING** — bot won't start without this"))
    results.append(("FOOTBALL_DATA_API_KEY", "✅" if api_key else "❌", f"Set (`…{api_key[-4:]}`)" if api_key else "**MISSING** — all API calls will fail"))

    # 2 — API connectivity
    import football_api as _fa
    try:
        raw = await _fa._get("/competitions/WC")
        if raw:
            results.append(("API /competitions/WC", "✅", f"OK — {raw.get('name', '?')}"))
        else:
            results.append(("API /competitions/WC", "❌", "Returned no data — check API key or plan tier"))
    except Exception as e:
        results.append(("API /competitions/WC", "❌", f"Exception: {e}"))

    # 3 — Match data
    try:
        today_m = await _fa.get_todays_matches()
        results.append(("Today's Matches API", "✅", f"{len(today_m)} match(es) today"))
    except Exception as e:
        results.append(("Today's Matches API", "⚠️", f"Failed: {e}"))

    # 4 — Match order cache
    order = get_wc_match_order()
    results.append(("WC Match Order Cache", "✅" if order else "⚠️", f"{len(order)} matches cached" if order else "Empty — use /reloadmatches to refresh"))

    # 5 — Background tasks
    results.append(("Monitor Loop", "✅" if monitor_loop.is_running() else "❌", "Running" if monitor_loop.is_running() else "STOPPED"))
    results.append(("Daily Summary Loop", "✅" if daily_summary_loop.is_running() else "❌", "Running" if daily_summary_loop.is_running() else "STOPPED"))

    # 6 — State / persistence
    try:
        guild_count = len(state.all_guild_configs())
        results.append(("State / Persistence", "✅", f"{guild_count} guild(s) configured"))
    except Exception as e:
        results.append(("State / Persistence", "❌", f"Error reading state: {e}"))

    # 7 — Guild config (if in a guild)
    if interaction.guild:
        gid = str(interaction.guild_id)
        cfg = state.get_guild_config(gid)
        has_live    = bool(cfg.get("channel_id"))
        has_pred    = bool(cfg.get("predictions_channel_id"))
        results.append(("This Guild: live channel",       "✅" if has_live else "⚠️", "Set" if has_live else "Not configured — run /setchannel"))
        results.append(("This Guild: predictions channel","✅" if has_pred else "⚠️", "Set" if has_pred else "Not configured — run /setpredictionschannel"))

    # Build embed
    em = discord.Embed(title="🔬 Full System Diagnostics — FIFA World Cup 2026 Bot", color=emb.C_BLUE)
    lines = [f"{status}  **{system}** — {detail}" for system, status, detail in results]
    # Split into chunks to stay within embed limits
    chunk = "\n".join(lines[:15])
    em.description = chunk
    if len(lines) > 15:
        em.add_field(name="Continued", value="\n".join(lines[15:]), inline=False)

    fail_count = sum(1 for _, s, _ in results if s == "❌")
    warn_count = sum(1 for _, s, _ in results if s == "⚠️")
    if fail_count:
        em.color = emb.C_RED
        em.set_footer(text=f"❌ {fail_count} failure(s), ⚠️ {warn_count} warning(s) — action required")
    elif warn_count:
        em.color = emb.C_ORANGE
        em.set_footer(text=f"⚠️ {warn_count} warning(s) — review recommended")
    else:
        em.color = emb.C_GREEN
        em.set_footer(text="✅ All systems operational")

    await interaction.followup.send(embed=em, ephemeral=True)


# req #25: /debug removed — use /debugmatch for per-match state inspection


# ── /reloadmatches ────────────────────────────────────────────────────────────

_register("Admin", "reloadmatches", "Reload the WC match order cache from the API")

@tree.command(name="reloadmatches", description="[Admin] Reload the WC match order cache from the API")
@app_commands.default_permissions(administrator=True)
async def slash_reloadmatches(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await load_wc_match_order()
        order = get_wc_match_order()
        await interaction.followup.send(
            embed=_success_embed(f"Match order cache reloaded — {len(order)} matches loaded."),
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(
            embed=_error_embed(f"Failed to reload match cache: {e}"),
            ephemeral=True,
        )


# ── Unified /testembed (replaces /testnotifications, /testpredictions, /testmotm)

_TEST_MID       = "test-unified-0001"
_TEST_MATCH_ID  = _TEST_MID
_TEST_HOME_TEAM = "Brazil"
_TEST_AWAY_TEAM = "Argentina"

# Keep legacy constants so any other references still resolve
_TEST_MATCH_ID_PRED = _TEST_MID
_TEST_HOME_TEAM_PRED = _TEST_HOME_TEAM
_TEST_AWAY_TEAM_PRED = _TEST_AWAY_TEAM
_TEST_MATCH_ID_MOTM = _TEST_MID
_TEST_MOTM_NOMINEES = [
    "Vinícius Jr.", "Raphinha", "Lautaro Martínez",
    "Lionel Messi", "Rodrygo", "Alexis Mac Allister",
    "Bruno Guimarães", "Jude Bellingham",
]

# Legacy list kept for any code that still references it
_TEST_NOTIF_TYPES = (
    "kickoff", "goal", "redcard", "halftime", "fulltime",
    "reminder_15", "extratime", "pso", "lineup", "recap", "daily",
)

# ── /testembed — unified admin test command ────────────────────────────────────

_register("Admin", "testembed", "Test any notification — set score/minute, preview or broadcast to channels")

_TESTEMBED_CHOICES = [
    app_commands.Choice(name="⏰ Reminder    — 60 min before kick-off",  value="reminder_60"),
    app_commands.Choice(name="⏰ Reminder    — 15 min before kick-off",  value="reminder_15"),
    app_commands.Choice(name="📋 Lineup      — confirmed starting XI",   value="lineup"),
    app_commands.Choice(name="🔔 Kick-off    — match started",           value="kickoff"),
    app_commands.Choice(name="⚽ Goal        — scored",                  value="goal"),
    app_commands.Choice(name="🟥 Red Card    — player dismissed",        value="red_card"),
    app_commands.Choice(name="⏱️ Half-time   — score at break",          value="halftime"),
    app_commands.Choice(name="▶️ 2nd Half    — second half started",     value="second_half"),
    app_commands.Choice(name="⏱️ Extra Time  — after 90+",               value="extra_time"),
    app_commands.Choice(name="🎯 Penalties   — shootout begins",         value="penalty_shootout"),
    app_commands.Choice(name="🏁 Full-time   — match ended",             value="fulltime"),
    app_commands.Choice(name="📺 Recap       — highlights + YT link",    value="recap"),
    app_commands.Choice(name="🎯 Prediction Poll  — open interactive vote",   value="prediction_open"),
    app_commands.Choice(name="📊 Prediction Results — resolve with score",    value="prediction_resolve"),
    app_commands.Choice(name="🌟 MOTM Vote   — open poll",               value="motm_open"),
    app_commands.Choice(name="🏆 MOTM Results — tally & reveal winner", value="motm_results"),
    app_commands.Choice(name="📅 Daily Summary — end-of-day recap",      value="daily_summary"),
    app_commands.Choice(name="🏆 Leaderboard — current standings",       value="leaderboard"),
    app_commands.Choice(name="🗑️  Clear       — wipe all test match state", value="clear"),
]

# ── Test-data builder helpers ──────────────────────────────────────────────────

def _build_test_match(
    home:    str = "Brazil",
    away:    str = "Argentina",
    h_score: int = 2,
    a_score: int = 1,
    minute_: int = 67,
    status:  str = "IN_PLAY",
) -> dict:
    winner = (
        "HOME_TEAM" if h_score > a_score else
        "AWAY_TEAM" if a_score > h_score else
        "DRAW"
    )
    return {
        "id":         _TEST_MID,
        "homeTeam":   {"id": 1, "name": home, "shortName": home, "tla": home[:3].upper()},
        "awayTeam":   {"id": 2, "name": away, "shortName": away, "tla": away[:3].upper()},
        "utcDate":    (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "stage":      "GROUP_STAGE",
        "group":      "GROUP_A",
        "status":     status,
        "minute":     minute_,
        "matchNumber": 1,
        "competition": {"name": "FIFA World Cup", "code": "WC"},
        "venue":       "Estadio Azteca, Mexico City",
        "score": {
            "fullTime":    {"home": h_score, "away": a_score},
            "halfTime":    {"home": min(h_score, 1), "away": 0},
            "regularTime": None,
            "extraTime":   None,
            "penalties":   None,
            "winner":      winner,
            "duration":    "REGULAR",
        },
    }


def _build_test_detail(match: dict) -> dict:
    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    h    = (match["score"]["fullTime"] or {}).get("home", 2)
    a    = (match["score"]["fullTime"] or {}).get("away", 1)
    goals: list[dict] = []
    if h >= 1:
        goals.append({"minute": "23", "type": "REGULAR",
                      "scorer": {"name": f"{home} Player A"}, "assist": {"name": f"{home} Player B"},
                      "team": match["homeTeam"]})
    if a >= 1:
        goals.append({"minute": "41", "type": "REGULAR",
                      "scorer": {"name": f"{away} Player X"}, "assist": None,
                      "team": match["awayTeam"]})
    if h >= 2:
        goals.append({"minute": "67", "type": "REGULAR",
                      "scorer": {"name": f"{home} Player C"}, "assist": {"name": f"{home} Player A"},
                      "team": match["homeTeam"]})
    if a >= 2:
        goals.append({"minute": "78", "type": "REGULAR",
                      "scorer": {"name": f"{away} Player Y"}, "assist": None,
                      "team": match["awayTeam"]})
    return {
        **match,
        "goals": goals,
        "bookings": [
            {"minute": "55", "type": "RED_CARD",
             "player": {"name": f"{away} Player Z"}, "team": match["awayTeam"]},
        ],
        "lineups": [
            {
                "team": match["homeTeam"],
                "formation": "4-3-3",
                "startXI": [{"player": {"name": f"{home} Player {i}"}} for i in range(1, 12)],
                "bench":   [{"player": {"name": f"{home} Sub {i}"}} for i in range(1, 4)],
            },
            {
                "team": match["awayTeam"],
                "formation": "4-4-2",
                "startXI": [{"player": {"name": f"{away} Player {i}"}} for i in range(1, 12)],
                "bench":   [{"player": {"name": f"{away} Sub {i}"}} for i in range(1, 4)],
            },
        ],
        "referees": [{"name": "Pierluigi Collina", "role": "REFEREE"}],
    }


# ── /testembed command ─────────────────────────────────────────────────────────

@tree.command(name="testembed", description="[Admin] Test any notification — set teams/score/minute, preview or broadcast")
@app_commands.describe(
    embed_type         = "Which notification type to test",
    home_team          = "Home team name (default: Brazil)",
    away_team          = "Away team name (default: Argentina)",
    home_score         = "Home score shown in the embed (default varies by type)",
    away_score         = "Away score shown in the embed (default varies by type)",
    minute             = "Match minute shown in the embed (default varies by type)",
    broadcast_to_channels = "Send to configured channels instead of showing only to you",
)
@app_commands.choices(embed_type=_TESTEMBED_CHOICES)
@app_commands.default_permissions(administrator=True)
async def slash_testembed(
    interaction: discord.Interaction,
    embed_type:              str,
    home_team:               str | None = None,
    away_team:               str | None = None,
    home_score:              int | None = None,
    away_score:              int | None = None,
    minute:                  int | None = None,
    broadcast_to_channels:   bool = False,
):
    if not interaction.guild:
        await interaction.response.send_message(embed=_error_embed("Use this inside a server."), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)

    # ── Resolve params with sensible defaults per embed type ──────────────────
    ht = home_team  or _TEST_HOME_TEAM
    at = away_team  or _TEST_AWAY_TEAM

    # Per-type score/minute defaults
    _defaults: dict[str, tuple[int, int, int, str]] = {
        # type              h  a  min   status
        "reminder_60":     (0, 0,   0,  "SCHEDULED"),
        "reminder_15":     (0, 0,   0,  "SCHEDULED"),
        "kickoff":         (0, 0,   1,  "IN_PLAY"),
        "lineup":          (0, 0,   0,  "SCHEDULED"),
        "goal":            (1, 0,  23,  "IN_PLAY"),
        "red_card":        (1, 0,  55,  "IN_PLAY"),
        "halftime":        (1, 0,  45,  "PAUSED"),
        "second_half":     (1, 0,  46,  "IN_PLAY"),
        "extra_time":      (1, 1, 101,  "EXTRA_TIME"),
        "penalty_shootout":(1, 1, 121,  "PENALTY_SHOOTOUT"),
        "fulltime":        (2, 1,  90,  "FINISHED"),
        "recap":           (2, 1,  90,  "FINISHED"),
        "prediction_open": (0, 0,   0,  "SCHEDULED"),
        "prediction_resolve": (2, 1, 90, "FINISHED"),
        "motm_open":       (2, 1,  76,  "IN_PLAY"),
        "motm_results":    (2, 1,  90,  "FINISHED"),
        "daily_summary":   (2, 1,  90,  "FINISHED"),
        "leaderboard":     (2, 1,  90,  "FINISHED"),
        "clear":           (0, 0,   0,  "SCHEDULED"),
    }
    dh, da, dm, dstatus = _defaults.get(embed_type, (2, 1, 67, "IN_PLAY"))
    h  = home_score if home_score is not None else dh
    a  = away_score if away_score is not None else da
    m  = minute     if minute     is not None else dm

    match  = _annotate_match(_build_test_match(ht, at, h, a, m, dstatus))
    detail = _build_test_detail(match)

    # ── Channel targets ───────────────────────────────────────────────────────
    live_cid  = cfg.get("channel_id")
    pred_cid  = cfg.get("predictions_channel_id") or cfg.get("channel_id")
    sum_cid   = cfg.get("summary_channel_id")     or cfg.get("channel_id")

    def _live_ch():
        return bot.get_channel(int(live_cid)) if live_cid else None

    def _pred_ch():
        return bot.get_channel(int(pred_cid)) if pred_cid else None

    def _sum_ch():
        return bot.get_channel(int(sum_cid)) if sum_cid else None

    async def _send(target_ch, embed_, view_=None, content_=None):
        """Send to channel OR show ephemeral based on broadcast_to_channels flag."""
        if broadcast_to_channels and target_ch and isinstance(target_ch, (discord.TextChannel, discord.Thread)):
            kw = {"embed": embed_}
            if view_:    kw["view"]    = view_
            if content_: kw["content"] = content_
            try:
                await target_ch.send(**kw)
                return True
            except discord.HTTPException as _e:
                log.warning("[TESTEMBED] Channel send failed: %s", _e)
                return False
        else:
            kw = {"embed": embed_, "ephemeral": True}
            if view_: kw["view"] = view_
            await interaction.followup.send(**kw)
            return True

    # ── Handle each embed type ────────────────────────────────────────────────

    if embed_type == "reminder_60":
        em = emb.embed_reminder(match, 60)
        sent = await _send(_live_ch(), em)

    elif embed_type == "reminder_15":
        em = emb.embed_reminder(match, 15)
        sent = await _send(_live_ch(), em)

    elif embed_type == "kickoff":
        em = emb.embed_kickoff(match, detail)
        sent = await _send(_live_ch(), em)

    elif embed_type == "lineup":
        em = emb.embed_lineups(match, detail)
        sent = await _send(_live_ch(), em)

    elif embed_type == "goal":
        goal_ev = {
            "minute": str(m), "type": "REGULAR",
            "scorer": {"name": f"{ht} Scorer"},
            "assist": {"name": f"{ht} Assist"},
            "team":   match["homeTeam"],
            "score":  {"home": h, "away": a},
        }
        em = emb.embed_goal(match, detail, goal_ev)
        sent = await _send(_live_ch(), em)

    elif embed_type == "red_card":
        card_ev = {
            "minute": str(m), "type": "RED_CARD",
            "player": {"name": f"{at} Defender"},
            "team":   match["awayTeam"],
        }
        em = emb.embed_red_card(match, detail, card_ev)
        sent = await _send(_live_ch(), em)

    elif embed_type == "halftime":
        em = emb.embed_halftime(match, detail)
        sent = await _send(_live_ch(), em)

    elif embed_type == "second_half":
        em = emb.embed_second_half(match)
        sent = await _send(_live_ch(), em)

    elif embed_type == "extra_time":
        et_match = {**match, "status": "EXTRA_TIME", "minute": m}
        em = emb.embed_extra_time(et_match, detail)
        sent = await _send(_live_ch(), em)

    elif embed_type == "penalty_shootout":
        pso_match = {**match, "status": "PENALTY_SHOOTOUT", "stage": "LAST_16", "group": None, "minute": m}
        em = emb.embed_penalty_shootout(pso_match, detail)
        sent = await _send(_live_ch(), em)

    elif embed_type == "fulltime":
        em = emb.embed_fulltime(match, detail)
        sent = await _send(_live_ch(), em)

    elif embed_type == "recap":
        yt_url = "https://www.youtube.com/results?search_query=FIFA+World+Cup+2026+highlights"
        em = emb.embed_full_recap(match, detail, yt_url)
        sent = await _send(_sum_ch(), em)

    elif embed_type == "prediction_open":
        em   = emb.embed_prediction_poll(match)
        view = PredictionView(_TEST_MID, ht, at, knockout=False)
        pred_ch = _pred_ch()
        if not pred_ch:
            await interaction.followup.send(embed=_error_embed("No predictions channel configured.", "Run `/setpredictionschannel` first."), ephemeral=True)
            return
        try:
            await pred_ch.send(content="🧪 **Admin test prediction poll**", embed=em, view=view)
            sent = True
        except discord.HTTPException as _e:
            await interaction.followup.send(embed=_error_embed(f"Failed: {_e}"), ephemeral=True)
            return

    elif embed_type == "prediction_resolve":
        # Resolve any predictions in state using h/a as the final score
        preds = state.get_score_predictions(_TEST_MID)
        exact_winners  = [(uid, p["home"], p["away"]) for uid, p in preds.items()
                         if p.get("home") == h and p.get("away") == a]
        result_home_win = h > a
        result_away_win = a > h
        result_winners = [
            (uid, p["home"], p["away"]) for uid, p in preds.items()
            if (uid, p["home"], p["away"]) not in exact_winners
            and (
                (result_home_win and p.get("home", 0) > p.get("away", 0)) or
                (result_away_win and p.get("away", 0) > p.get("home", 0)) or
                (not result_home_win and not result_away_win and p.get("home", 0) == p.get("away", 0))
            )
        ]
        for uid, _, _ in exact_winners:
            state.update_leaderboard(gid, uid, 3)
        for uid, _, _ in result_winners:
            state.update_leaderboard(gid, uid, 1)
        em = emb.embed_prediction_results(match, h, a, exact_winners, result_winners)
        pred_ch = _pred_ch()
        if pred_ch:
            try:
                await pred_ch.send(content="🧪 **Test prediction results:**", embed=em)
                sent = True
            except discord.HTTPException:
                sent = False
        else:
            sent = False
            await interaction.followup.send(embed=em, ephemeral=True)

    elif embed_type == "motm_open":
        nominees = _build_motm_nominees(detail)
        if not nominees:
            nominees = _build_motm_fallback(match)
        if not nominees:
            nominees = _TEST_MOTM_NOMINEES
        em   = emb.embed_motm_vote(match, nominees)
        view = MotmVoteView(_TEST_MID, nominees)
        pred_ch = _pred_ch()
        if not pred_ch:
            await interaction.followup.send(embed=_error_embed("No predictions channel configured.", "Run `/setpredictionschannel` first."), ephemeral=True)
            return
        try:
            msg = await pred_ch.send(content="🧪 **Admin test MOTM vote**", embed=em, view=view)
            state.save_motm_message_id(_TEST_MID, gid, msg.id)
            sent = True
        except discord.HTTPException as _e:
            await interaction.followup.send(embed=_error_embed(f"Failed: {_e}"), ephemeral=True)
            return

    elif embed_type == "motm_results":
        votes = state.get_motm_votes(_TEST_MID, gid)
        if not votes:
            await interaction.followup.send(
                embed=_error_embed("No MOTM votes found.", "Run `embed_type=motm_open` first to collect votes."),
                ephemeral=True,
            )
            return
        tally: dict[str, int] = {}
        for _, player in votes.items():
            tally[player] = tally.get(player, 0) + 1
        max_v   = max(tally.values())
        winners = [p for p, v in tally.items() if v == max_v]
        for uid, player in votes.items():
            if player in winners:
                state.update_leaderboard(gid, uid, 1)
        em      = emb.embed_motm_result(match, winners, tally)
        pred_ch = _pred_ch()
        # Lock poll message if it exists
        motm_msg_id = state.get_motm_message_id(_TEST_MID, gid)
        if pred_ch and motm_msg_id:
            try:
                motm_msg = await pred_ch.fetch_message(int(motm_msg_id))
                if motm_msg.embeds:
                    locked = motm_msg.embeds[0].copy()
                    locked.set_footer(text="🔒 Voting closed (test)  •  🏆 FIFA World Cup 2026")
                    await motm_msg.edit(embed=locked, view=None)
            except discord.HTTPException:
                pass
        sent = await _send(_pred_ch(), em)

    elif embed_type == "daily_summary":
        fake_matches = [_annotate_match(_build_test_match(ht, at, h, a, 90, "FINISHED"))]
        em   = emb.embed_daily_summary(fake_matches, datetime.now(timezone.utc).strftime("%d %B %Y"))
        sent = await _send(_sum_ch(), em)

    elif embed_type == "leaderboard":
        lb = state.get_leaderboard(gid)
        if lb:
            em = emb.embed_leaderboard(interaction.guild, lb)
        else:
            fake_lb = {
                "111111111": {"points": 21, "exact": 5, "correct": 11, "streak": 6, "best_streak": 6, "total_predictions": 18, "monthly": {}},
                "222222222": {"points": 14, "exact": 3, "correct":  8, "streak": 4, "best_streak": 5, "total_predictions": 12, "monthly": {}},
                str(interaction.user.id): {"points": 9, "exact": 1, "correct": 6, "streak": 1, "best_streak": 3, "total_predictions": 9, "monthly": {}},
            }
            em = emb.embed_leaderboard(interaction.guild, fake_lb)
        sent = await _send(_pred_ch(), em)

    elif embed_type == "clear":
        state.clear_match_state(_TEST_MID)
        await interaction.followup.send(
            embed=_success_embed(f"All test match state cleared for `{_TEST_MID}`."),
            ephemeral=True,
        )
        return

    else:
        await interaction.followup.send(embed=_error_embed(f"Unknown embed type: `{embed_type}`"), ephemeral=True)
        return

    # ── Confirmation message ──────────────────────────────────────────────────
    dest  = "your configured channels" if broadcast_to_channels else "you only (preview)"
    score = f"{h}–{a}"
    info_lines = [
        f"**Type:** `{embed_type}`",
        f"**Teams:** {ht} vs {at}",
        f"**Score:** {score}   **Minute:** {m}",
        f"**Sent to:** {dest}",
    ]
    if embed_type in ("prediction_open", "motm_open"):
        info_lines.append(f"Use **`embed_type=prediction_resolve`** / **`motm_results`** to close the poll.")
    await interaction.followup.send(
        embed=discord.Embed(
            title="🧪 Test sent",
            description="\n".join(info_lines),
            color=emb.C_BLUE,
        ).set_footer(text="Only you can see this confirmation."),
        ephemeral=True,
    )


# ── !testembed prefix alias ────────────────────────────────────────────────────

_TEST_TYPES = tuple(c.value for c in _TESTEMBED_CHOICES)

@bot.command(name="testembed", help="[Admin] Preview a notification embed. Usage: !testembed goal")
@commands.has_permissions(administrator=True)
async def prefix_testembed(ctx, embed_type: str = "goal"):
    valid = _TEST_TYPES
    if embed_type not in valid:
        await ctx.send(embed=_error_embed(
            f"Unknown type `{embed_type}`.",
            "Valid types: " + ", ".join(f"`{t}`" for t in valid),
        ))
        return
    fake = _annotate_match(_build_test_match())
    det  = _build_test_detail(fake)
    if embed_type == "prediction_open":
        view = PredictionView(_TEST_MID, _TEST_HOME_TEAM, _TEST_AWAY_TEAM, knockout=False)
        await ctx.send(embed=emb.embed_prediction_poll(fake), view=view)
    elif embed_type == "lineup":
        await ctx.send(embed=emb.embed_lineups(fake, det))
    elif embed_type == "goal":
        ev = {"minute": "23", "type": "REGULAR", "scorer": {"name": "Test Scorer"},
              "assist": {"name": "Test Assist"}, "team": fake["homeTeam"], "score": {"home": 1, "away": 0}}
        await ctx.send(embed=emb.embed_goal(fake, det, ev))
    else:
        await ctx.send(embed=emb.embed_test(embed_type))



# ═══════════════════════════════════════════════════════════════════════════════
#  PREFIX COMMAND ALIASES (minimal set for legacy support)
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="setmatcheschannel")
@commands.has_permissions(manage_channels=True)
async def prefix_setmatcheschannel(ctx):
    state.set_channel_id(str(ctx.guild.id), "channel_id", str(ctx.channel.id))
    await ctx.send(embed=_success_embed(f"**#{ctx.channel.name}** is now the live match alerts channel."))


# req #25: !setmode prefix command removed — use /setup instead


# ═══════════════════════════════════════════════════════════════════════════════
#  CONTENT FEATURES — /player  /matchfacts  /tournamentstats
# ═══════════════════════════════════════════════════════════════════════════════

# ── /player ───────────────────────────────────────────────────────────────────

_register("Info", "player", "Look up a player's World Cup stats")

@tree.command(name="player", description="Look up a player's World Cup 2026 stats")
@app_commands.describe(name="Player name (e.g. Mbappé, Vinicius, Bellingham)")
async def slash_player(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    entry = await get_player_wc_stats(name)
    if not entry:
        await interaction.followup.send(
            embed=_error_embed(
                f"**{name}** not found in the WC 2026 scorers list.\n"
                "Only players who have scored at least once appear here. "
                "Try a different spelling or first/last name only."
            ),
            ephemeral=True,
        )
        return

    player  = entry.get("player") or {}
    team    = entry.get("team")   or {}
    goals   = entry.get("goals",      0) or 0
    assists = entry.get("assists",     0) or 0
    pens    = entry.get("penalties",   0) or 0

    flag       = team_flag(team)
    team_name  = team_display(team)
    nationality = player.get("nationality") or "—"
    position    = player.get("position") or "—"

    em = discord.Embed(
        title=f"⚽ {player.get('name', name)}",
        colour=discord.Colour.gold(),
    )
    em.add_field(name="🏳️ Nationality",   value=nationality,         inline=True)
    em.add_field(name="🎽 Position",       value=position,            inline=True)
    em.add_field(name=f"{flag} Club / NT", value=team_name,           inline=True)
    em.add_field(name="⚽ Goals",          value=str(goals),          inline=True)
    em.add_field(name="🎯 Assists",        value=str(assists),        inline=True)
    em.add_field(name="🅿️ Penalties",      value=str(pens),           inline=True)

    non_pen = goals - pens
    if non_pen > 0:
        em.add_field(name="🥅 Non-pen goals", value=str(non_pen), inline=True)

    em.set_footer(text="Stats from football-data.org · WC 2026")
    await interaction.followup.send(embed=em, ephemeral=True)


# ── /matchfacts ────────────────────────────────────────────────────────────────

_register("Info", "matchfacts", "Goals, cards & full facts for a match — use autocomplete to pick one")

@tree.command(name="matchfacts", description="Full facts & events for a World Cup match (by match ID)")
@app_commands.describe(match_id="Pick a match from the list or type a match ID")
@app_commands.autocomplete(match_id=autocomplete_match_id)
async def slash_matchfacts(interaction: discord.Interaction, match_id: int):
    await interaction.response.defer(ephemeral=True)
    stats = await get_match_stats(match_id)
    if not stats:
        await interaction.followup.send(
            embed=_error_embed(f"Match **{match_id}** not found. Use `/schedule` or `/today` to get a valid match ID."),
            ephemeral=True,
        )
        return

    detail    = stats["detail"]
    home_team = detail.get("homeTeam") or {}
    away_team = detail.get("awayTeam") or {}
    status    = detail.get("status", "UNKNOWN")
    stage     = STAGE_NAMES.get(detail.get("stage", ""), detail.get("stage", "—"))
    venue     = (detail.get("venue") or "—")
    referee   = (detail.get("referees") or [{}])[0].get("name", "—")

    h_score, a_score = get_score(detail, "fullTime")
    h_disp = team_display(home_team)
    a_disp = team_display(away_team)
    h_flag = team_flag(home_team)
    a_flag = team_flag(away_team)

    score_str = (
        f"{h_flag} **{h_disp}** {h_score} – {a_score} **{a_disp}** {a_flag}"
        if h_score is not None
        else f"{h_flag} **{h_disp}** vs **{a_disp}** {a_flag} *(not yet played)*"
    )

    num_str = _match_num_str(match_id)
    title_num = f" — Match {num_str}" if num_str else f" — ID {match_id}"
    em = discord.Embed(
        title=f"📋 Match Facts{title_num}",
        description=score_str,
        colour=discord.Colour.blue(),
    )
    matchday = detail.get("matchday")
    md_str   = f"Matchday {matchday}" if matchday else "—"
    em.add_field(name="📅 Matchday", value=md_str,  inline=True)
    em.add_field(name="🏟️ Stage",   value=stage,   inline=True)
    em.add_field(name="📍 Venue",   value=venue,   inline=True)
    em.add_field(name="🟨 Referee", value=referee, inline=True)

    # Goals
    def _goal_lines(goal_list: list[dict]) -> str:
        if not goal_list:
            return "—"
        lines = []
        for g in goal_list:
            scorer  = (g.get("scorer")  or {}).get("name", "?")
            minute  = g.get("minute",   "?")
            extra   = g.get("injuryTime")
            typ     = g.get("type", "")
            icon    = "🅿️" if typ == "PENALTY" else ("🔁" if typ == "OWN" else "⚽")
            t_str   = f"{minute}'" + (f"+{extra}" if extra else "")
            lines.append(f"{icon} {scorer} {t_str}")
        return "\n".join(lines)

    em.add_field(
        name=f"{h_flag} {h_disp} Goals ({len(stats['home_goals'])})",
        value=_goal_lines(stats["home_goals"]),
        inline=True,
    )
    em.add_field(
        name=f"{a_flag} {a_disp} Goals ({len(stats['away_goals'])})",
        value=_goal_lines(stats["away_goals"]),
        inline=True,
    )
    em.add_field(name="\u200b", value="\u200b", inline=False)  # spacer

    # Cards
    def _card_summary(yellows: list, reds: list) -> str:
        parts = []
        if yellows:
            names = ", ".join((b.get("player") or {}).get("name", "?") for b in yellows)
            parts.append(f"🟨 {len(yellows)} — {names}")
        if reds:
            names = ", ".join((b.get("player") or {}).get("name", "?") for b in reds)
            parts.append(f"🟥 {len(reds)} — {names}")
        return "\n".join(parts) if parts else "None"

    em.add_field(
        name=f"{h_flag} {h_disp} Cards",
        value=_card_summary(stats["home_yellows"], stats["home_reds"]),
        inline=True,
    )
    em.add_field(
        name=f"{a_flag} {a_disp} Cards",
        value=_card_summary(stats["away_yellows"], stats["away_reds"]),
        inline=True,
    )
    em.add_field(name="\u200b", value="\u200b", inline=False)

    # Subs
    def _sub_lines(sub_list: list[dict]) -> str:
        if not sub_list:
            return "—"
        lines = []
        for s in sub_list[:5]:  # cap at 5 to avoid embed overflow
            out_p = (s.get("playerOut") or {}).get("name", "?")
            in_p  = (s.get("playerIn")  or {}).get("name", "?")
            minute = s.get("minute", "?")
            lines.append(f"🔄 {out_p} → {in_p} {minute}'")
        return "\n".join(lines)

    em.add_field(
        name=f"{h_flag} {h_disp} Subs ({len(stats['home_subs'])})",
        value=_sub_lines(stats["home_subs"]),
        inline=True,
    )
    em.add_field(
        name=f"{a_flag} {a_disp} Subs ({len(stats['away_subs'])})",
        value=_sub_lines(stats["away_subs"]),
        inline=True,
    )

    # Lineups
    def _lineup_str(lineup: dict) -> str:
        xi = lineup.get("startXI") or []
        if not xi:
            return "Not available"
        names = [p.get("player", {}).get("name", "?") for p in xi[:11]]
        formation = lineup.get("formation", "")
        header = f"Formation: {formation}\n" if formation else ""
        return header + " · ".join(names)

    if stats["home_lineup"] or stats["away_lineup"]:
        em.add_field(name="\u200b", value="\u200b", inline=False)
        em.add_field(
            name=f"{h_flag} {h_disp} Starting XI",
            value=_lineup_str(stats["home_lineup"])[:512],
            inline=False,
        )
        em.add_field(
            name=f"{a_flag} {a_disp} Starting XI",
            value=_lineup_str(stats["away_lineup"])[:512],
            inline=False,
        )

    em.set_footer(text=f"Status: {status} · football-data.org · WC 2026")
    await interaction.followup.send(embed=em, ephemeral=True)


# ── /tournamentstats ───────────────────────────────────────────────────────────

_register("Info", "tournamentstats", "WC 2026 tournament-wide stats dashboard")

@tree.command(name="tournamentstats", description="World Cup 2026 tournament stats: top scorers, goals, clean sheets")
async def slash_tournament_stats(interaction: discord.Interaction):
    await interaction.response.defer()
    ts = await get_tournament_stats()

    played = ts["total_matches_played"]
    goals  = ts["total_goals"]
    gpg    = ts["goals_per_game"]

    em = discord.Embed(
        title="🏆 FIFA World Cup 2026 — Tournament Stats",
        colour=discord.Colour.gold(),
    )

    # Headline numbers
    em.add_field(name="⚽ Total Goals",      value=str(goals),  inline=True)
    em.add_field(name="🎮 Matches Played",   value=str(played), inline=True)
    em.add_field(name="📊 Goals / Game",     value=str(gpg),    inline=True)

    # Most goals in a single match
    mgm = ts.get("most_goals_match")
    if mgm:
        h_t   = team_display(mgm.get("homeTeam") or {})
        a_t   = team_display(mgm.get("awayTeam") or {})
        h_f   = team_flag(mgm.get("homeTeam") or {})
        a_f   = team_flag(mgm.get("awayTeam") or {})
        h_s, a_s = get_score(mgm, "fullTime")
        mgm_num  = _match_num_str(mgm.get("id", 0))
        mgm_md   = mgm.get("matchday")
        num_tag  = f" (Match {mgm_num}" + (f" · MD{mgm_md}" if mgm_md else "") + ")" if mgm_num else (f" (MD{mgm_md})" if mgm_md else "")
        em.add_field(
            name=f"🔥 Most Goals in a Match ({ts['most_goals_match_count']})",
            value=f"{h_f} {h_t} {h_s}–{a_s} {a_t} {a_f}{num_tag}",
            inline=False,
        )

    # Top scorers
    scorers = ts.get("top_scorers", [])
    if scorers:
        lines = []
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, entry in enumerate(scorers[:5]):
            p   = entry.get("player") or {}
            t   = entry.get("team")   or {}
            g   = entry.get("goals", 0) or 0
            a   = entry.get("assists", 0) or 0
            flag = team_flag(t)
            medal = medals[i] if i < len(medals) else f"{i+1}."
            lines.append(f"{medal} {flag} **{p.get('name','?')}** — {g}⚽ {a}🎯")
        em.add_field(name="🏅 Top Scorers", value="\n".join(lines), inline=False)

    # Tightest defences (fewest goals conceded)
    leaders = ts.get("clean_sheet_leaders", [])
    if leaders:
        lines = []
        for entry in leaders[:5]:
            t    = entry["team"]
            flag = team_flag(t)
            name = team_display(t)
            ga   = entry["ga"]
            mp   = entry["played"]
            lines.append(f"{flag} **{name}** — {ga} GA in {mp} games")
        em.add_field(name="🧱 Tightest Defences (fewest goals conceded)", value="\n".join(lines), inline=False)

    if played == 0:
        em.description = "*The tournament hasn't started yet — check back after the opening match!*"

    em.set_footer(text="Live data · football-data.org · WC 2026")
    await interaction.followup.send(embed=em)


# ═══════════════════════════════════════════════════════════════════════════════
#  GENERAL / UTILITY COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

# ── /ping ─────────────────────────────────────────────────────────────────────

_register("Info", "ping", "Check bot latency and API response time")

@tree.command(name="ping", description="Check bot latency and API response time")
async def slash_ping(interaction: discord.Interaction):
    import time
    ws_ms = round(bot.latency * 1000)
    await interaction.response.defer(ephemeral=True)
    t0 = time.monotonic()
    try:
        await get_todays_matches()
        api_ms: int | str = round((time.monotonic() - t0) * 1000)
        api_status = "✅"
    except Exception:
        api_ms = "—"
        api_status = "❌"

    colour = emb.C_GREEN if ws_ms < 200 else (emb.C_GOLD if ws_ms < 500 else emb.C_RED)
    em = discord.Embed(title="🏓 Pong!", colour=colour)
    em.add_field(name="🔌 WebSocket latency", value=f"`{ws_ms} ms`",       inline=True)
    em.add_field(name=f"{api_status} API round-trip",  value=f"`{api_ms} ms`" if isinstance(api_ms, int) else f"`{api_ms}`", inline=True)
    em.set_footer(text="🏆 FIFA World Cup 2026 Bot")
    await interaction.followup.send(embed=em, ephemeral=True)


# ── /about ────────────────────────────────────────────────────────────────────

_register("Info", "about", "About this bot — features and credits")

@tree.command(name="about", description="About the FIFA World Cup 2026 Bot")
async def slash_about(interaction: discord.Interaction):
    total_cmds = sum(len(v) for v in COMMAND_REGISTRY.values())
    guilds     = len(bot.guilds)
    em = discord.Embed(
        title="⚽ FIFA World Cup 2026 Bot",
        description=(
            "Your all-in-one companion for the **2026 FIFA World Cup** 🏆\n"
            "Live alerts · Predictions · Leaderboards · Match stats · Player lookup"
        ),
        colour=discord.Colour.gold(),
    )
    em.add_field(name="🏆 Tournament",    value="FIFA World Cup 2026\nUSA · Canada · Mexico", inline=True)
    em.add_field(name="📅 Kicks off",     value="11 June 2026",                               inline=True)
    em.add_field(name="🏟️ Teams",         value="48 nations",                                 inline=True)
    em.add_field(name="📋 Slash commands",value=f"{total_cmds} available",                    inline=True)
    em.add_field(name="🖥️ Servers",       value=f"{guilds} server{'s' if guilds != 1 else ''}", inline=True)
    em.add_field(name="⚡ Data",           value="Live · Updated every 60 s",                  inline=True)
    em.add_field(
        name="🛠️ Stack",
        value="discord.py 2 · football-data.org API · Python 3.12",
        inline=False,
    )
    em.set_footer(text="Use /help to browse all commands  •  /setup to configure your server")
    await interaction.response.send_message(embed=em, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN — /testmatch (req #24)
# ═══════════════════════════════════════════════════════════════════════════════

_register("Admin", "testmatch", "Step-through a full match lifecycle to verify all embeds fire correctly")

@tree.command(name="testmatch", description="[Admin] Step through match lifecycle events to test all embeds")
@app_commands.describe(
    match_id="Match ID to simulate (0 = use a dummy test match)",
    step="Which lifecycle step to trigger",
)
@app_commands.choices(step=[
    app_commands.Choice(name="1 — Reminder (60 min)",        value="reminder_60"),
    app_commands.Choice(name="2 — Reminder (15 min)",        value="reminder_15"),
    app_commands.Choice(name="3 — Kickoff",                  value="kickoff"),
    app_commands.Choice(name="4 — Lineups",                  value="lineup"),
    app_commands.Choice(name="5 — Goal",                     value="goal"),
    app_commands.Choice(name="6 — Red card",                 value="red_card"),
    app_commands.Choice(name="7 — Half time",                value="halftime"),
    app_commands.Choice(name="8 — Second half start",        value="second_half"),
    app_commands.Choice(name="9 — Extra time",               value="extra_time"),
    app_commands.Choice(name="10 — Penalty shootout",        value="penalty_shootout"),
    app_commands.Choice(name="11 — Full time",               value="fulltime"),
    app_commands.Choice(name="12 — MOTM poll",               value="motm_open"),
    app_commands.Choice(name="13 — MOTM result",             value="motm_results"),
    app_commands.Choice(name="14 — Prediction poll",         value="prediction_open"),
    app_commands.Choice(name="15 — Match recap",             value="recap"),
])
@app_commands.default_permissions(administrator=True)
async def slash_testmatch(
    interaction: discord.Interaction,
    step: str,
    match_id: int = 0,
):
    """req #24: step through individual match-lifecycle events."""
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("Use this inside a server."), ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)

    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)

    if match_id != 0:
        try:
            raw = await get_match_detail(match_id)
            match  = _annotate_match(raw.get("match") or raw)
            detail = raw
        except Exception as e:
            await interaction.followup.send(
                embed=_error_embed(f"Could not fetch match `{match_id}`: {e}"),
                ephemeral=True,
            )
            return
    else:
        _defaults = {
            "reminder_60": (0, 0,  0,  "SCHEDULED"),
            "reminder_15": (0, 0,  0,  "SCHEDULED"),
            "kickoff":     (0, 0,  1,  "IN_PLAY"),
            "lineup":      (0, 0,  0,  "SCHEDULED"),
            "goal":        (1, 0, 23,  "IN_PLAY"),
            "red_card":    (1, 0, 55,  "IN_PLAY"),
            "halftime":    (1, 0, 45,  "PAUSED"),
            "second_half": (1, 0, 46,  "IN_PLAY"),
            "extra_time":  (1, 1,101,  "EXTRA_TIME"),
            "penalty_shootout": (1, 1, 121, "PENALTY_SHOOTOUT"),
            "fulltime":    (2, 1, 90,  "FINISHED"),
            "motm_open":   (2, 1, 76,  "IN_PLAY"),
            "motm_results":(2, 1, 90,  "FINISHED"),
            "prediction_open": (0, 0, 0, "SCHEDULED"),
            "recap":       (2, 1, 90,  "FINISHED"),
        }
        dh, da, dm, dstatus = _defaults.get(step, (1, 0, 45, "IN_PLAY"))
        match  = _annotate_match(_build_test_match(
            _TEST_HOME_TEAM, _TEST_AWAY_TEAM, dh, da, dm, dstatus
        ))
        detail = _build_test_detail(match)

    live_cid = cfg.get("channel_id")
    pred_cid = cfg.get("predictions_channel_id") or cfg.get("channel_id")
    live_ch  = bot.get_channel(int(live_cid))  if live_cid else None
    pred_ch  = bot.get_channel(int(pred_cid))  if pred_cid else None

    sent: list[str] = []
    try:
        if step == "reminder_60":
            em = emb.embed_reminder(match, 60)
            if live_ch: await live_ch.send(embed=em); sent.append("reminder-60 → live channel")
        elif step == "reminder_15":
            em = emb.embed_reminder(match, 15)
            if live_ch: await live_ch.send(embed=em); sent.append("reminder-15 → live channel")
        elif step == "kickoff":
            em = emb.embed_kickoff(match, detail)
            if live_ch: await live_ch.send(embed=em); sent.append("kickoff → live channel")
        elif step == "lineup":
            em = emb.embed_lineups(match, detail)
            if live_ch: await live_ch.send(embed=em); sent.append("lineup → live channel")
        elif step == "goal":
            ev = {"minute": "23", "type": "REGULAR",
                  "scorer": {"name": "Test Scorer"}, "assist": {"name": "Test Assist"},
                  "team": match["homeTeam"], "score": {"home": 1, "away": 0}}
            em = emb.embed_goal(match, detail, ev)
            if live_ch: await live_ch.send(embed=em); sent.append("goal → live channel")
        elif step == "red_card":
            ev = {"minute": "55", "type": "RED_CARD",
                  "player": {"name": "Test Defender"}, "team": match["awayTeam"]}
            em = emb.embed_red_card(match, detail, ev)
            if live_ch: await live_ch.send(embed=em); sent.append("red card → live channel")
        elif step == "halftime":
            em = emb.embed_halftime(match, detail)
            if live_ch: await live_ch.send(embed=em); sent.append("HT → live channel")
        elif step == "second_half":
            em = emb.embed_second_half(match)
            if live_ch: await live_ch.send(embed=em); sent.append("2H start → live channel")
        elif step == "extra_time":
            em = emb.embed_extra_time(match)
            if live_ch: await live_ch.send(embed=em); sent.append("ET → live channel")
        elif step == "penalty_shootout":
            em = emb.embed_pso(match)
            if live_ch: await live_ch.send(embed=em); sent.append("PSO → live channel")
        elif step == "fulltime":
            em = emb.embed_fulltime(match, detail)
            if live_ch: await live_ch.send(embed=em); sent.append("FT → live channel")
        elif step == "motm_open":
            nominees = _build_motm_nominees(detail) or ["Player A", "Player B", "Player C"]
            em   = emb.embed_motm_vote(match, nominees[:10])
            view = MotmVoteView(str(match.get("id", 0)), nominees[:10])
            if pred_ch: await pred_ch.send(embed=em, view=view); sent.append("MOTM poll → predictions channel")
        elif step == "motm_results":
            em = emb.embed_motm_result(match, ["Test Winner"], {"Test Winner": 5, "Runner Up": 2})
            if pred_ch: await pred_ch.send(embed=em); sent.append("MOTM result → predictions channel")
        elif step == "prediction_open":
            mid_str = str(match.get("id", 0))
            home    = team_display(match.get("homeTeam", {}))
            away    = team_display(match.get("awayTeam", {}))
            ko      = is_knockout(match)
            em      = emb.embed_prediction_poll(match)
            view    = PredictionView(mid_str, home, away, ko)
            if pred_ch: await pred_ch.send(embed=em, view=view); sent.append("prediction poll → predictions channel")
        elif step == "recap":
            em = emb.embed_full_recap(match, detail, None)
            if live_ch: await live_ch.send(embed=em); sent.append("recap → live channel")
    except discord.HTTPException as _e:
        await interaction.followup.send(
            embed=_error_embed(f"Send failed: {_e}"), ephemeral=True
        )
        return

    result_lines = [f"✅ {s}" for s in sent] or ["⚠️ No channel configured — nothing sent."]
    await interaction.followup.send(
        embed=discord.Embed(
            title=f"🧪 /testmatch — {step}",
            description="\n".join(result_lines),
            color=emb.C_GREEN,
        ).set_footer(text="Sent to configured channels"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN — TEST & TROUBLESHOOT COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

_register("Admin", "testchannels",    "Send a test ping to every configured channel")
_register("Admin", "debugmatch",      "Inspect full bot state for a match by ID")
_register("Admin", "testthread",      "Force-create (or find) the live thread for a match")
_register("Admin", "forcestandings",  "Force-post + pin standings to the summary channel now")
_register("Admin", "forceleaderboard","Force-post + pin leaderboard to the predictions channel now")
_register("Admin", "clearsentevents", "Clear sent-event flags for a match (re-enables test broadcasts)")


@tree.command(name="testchannels", description="[Admin] Ping every configured channel to verify bot access")
@app_commands.default_permissions(manage_guild=True)
async def slash_testchannels(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(embed=_error_embed("Use inside a server."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)

    channel_map = {
        "Live matches (`channel_id`)":       cfg.get("channel_id"),
        "Predictions (`predictions_channel_id`)": cfg.get("predictions_channel_id"),
        "Summary (`summary_channel_id`)":    cfg.get("summary_channel_id"),
        "Commands (`commands_channel_id`)":  cfg.get("commands_channel_id"),
    }

    lines: list[str] = []
    for label, cid in channel_map.items():
        if not cid:
            lines.append(f"⚪ **{label}** — not configured")
            continue
        ch = bot.get_channel(int(cid))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            lines.append(f"❌ **{label}** — channel `{cid}` not found / no access")
            continue
        try:
            test_em = discord.Embed(
                title="🔧 Channel Test",
                description=f"✅ Bot can reach this channel.\n**Role:** {label}",
                color=emb.C_GREEN,
            ).set_footer(text="Admin test via /testchannels")
            await ch.send(embed=test_em, delete_after=30)
            lines.append(f"✅ **{label}** — {ch.mention} OK (auto-deletes in 30 s)")
        except discord.HTTPException as e:
            lines.append(f"❌ **{label}** — {ch.mention} send failed: `{e}`")

    mode = cfg.get("mode", "detailed")  # req #23: read from cfg, not state.get_mode
    lines.append(f"\n📡 **Current mode:** `{mode}`")
    lines.append(f"🔄 **Monitor loop running:** `{monitor_loop.is_running()}`")

    em = discord.Embed(
        title="🔧 Channel Test Results",
        description="\n".join(lines),
        color=emb.C_BLUE,
    )
    await interaction.followup.send(embed=em, ephemeral=True)


@tree.command(name="debugmatch", description="[Admin] Inspect full bot state for a match ID")
@app_commands.describe(match_id="Match ID to inspect")
@app_commands.default_permissions(manage_guild=True)
async def slash_debugmatch(interaction: discord.Interaction, match_id: int):
    if not interaction.guild:
        await interaction.response.send_message(embed=_error_embed("Use inside a server."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    mid = str(match_id)

    snap     = state.get_snapshot(mid)
    sent     = state.get_sent(mid)
    preds    = state.get_score_predictions(mid)
    thread_id = state.get_match_thread(mid, gid)
    poll_id  = state.get_prediction_poll_message(mid, gid)
    motm_id  = state.get_motm_message_id(mid, gid)
    rem_sent = {m: state.is_reminder_sent(mid, m) for m in (15, 60, 90)}
    lb_pin   = state.get_pinned_leaderboard(gid)
    std_pin  = state.get_pinned_standings(gid)
    eod_pin  = state.get_pinned_eod_summary(gid)

    thread_status = "—"
    if thread_id:
        t = bot.get_channel(int(thread_id))
        thread_status = f"`{thread_id}` — {'✅ cached' if t else '⚠️ not in cache (may still exist)'}"

    em = discord.Embed(
        title=f"🔍 Match State — ID `{match_id}`",
        color=emb.C_BLUE,
    )
    em.add_field(name="Status snapshot",  value=f"`{snap.get('status','—')}` | {snap.get('home_score','?')}–{snap.get('away_score','?')}", inline=False)
    em.add_field(name="Sent events",      value=f"`{', '.join(sent) or 'none'}`",  inline=False)
    em.add_field(name="Reminders sent",   value="\n".join(f"  {m} min: `{'yes' if v else 'no'}`" for m, v in rem_sent.items()), inline=False)
    em.add_field(name="Predictions",      value=f"{len(preds)} submitted",          inline=True)
    em.add_field(name="Poll msg ID",      value=f"`{poll_id or '—'}`",              inline=True)
    em.add_field(name="MOTM msg ID",      value=f"`{motm_id or '—'}`",              inline=True)
    em.add_field(name="Match thread",     value=thread_status,                      inline=False)
    em.add_field(name="Pinned leaderboard", value=f"`{lb_pin or '—'}`",            inline=True)
    em.add_field(name="Pinned standings", value=f"`{std_pin or '—'}`",             inline=True)
    em.add_field(name="Pinned EOD summary",value=f"`{eod_pin or '—'}`",            inline=True)
    em.set_footer(text=f"Guild {gid}  •  Use /clearsentevents to reset sent flags")
    await interaction.followup.send(embed=em, ephemeral=True)


@tree.command(name="testthread", description="[Admin] Force-create (or find) the live thread for a match")
@app_commands.describe(match_id="Match ID to create/find a thread for")
@app_commands.default_permissions(manage_guild=True)
async def slash_testthread(interaction: discord.Interaction, match_id: int):
    if not interaction.guild:
        await interaction.response.send_message(embed=_error_embed("Use inside a server."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)

    try:
        detail = await get_match_detail(match_id)
    except Exception as e:
        await interaction.followup.send(embed=_error_embed(f"API error: {e}"), ephemeral=True)
        return
    if not detail:
        await interaction.followup.send(embed=_error_embed(f"Match `{match_id}` not found."), ephemeral=True)
        return

    thread = await _get_or_create_match_thread(detail, gid, cfg)
    if thread:
        em = discord.Embed(
            title="🧵 Match Thread",
            description=f"✅ Thread ready: {thread.mention}\n**Name:** {thread.name}\n**ID:** `{thread.id}`",
            color=emb.C_GREEN,
        )
        test_em = discord.Embed(
            title="🔧 Thread Test",
            description="This thread was created/verified by `/testthread`.",
            color=emb.C_BLUE,
        ).set_footer(text="Admin test — you can delete this message")
        await thread.send(embed=test_em)
    else:
        live_cid = cfg.get("channel_id")
        em = discord.Embed(
            title="❌ Thread Creation Failed",
            description=(
                f"Could not create thread for match `{match_id}`.\n"
                f"**Live channel configured:** `{'Yes — ' + live_cid if live_cid else 'No — run /setmatcheschannel first'}`"
            ),
            color=emb.C_RED,
        )
    await interaction.followup.send(embed=em, ephemeral=True)


@tree.command(name="forcestandings", description="[Admin] Immediately fetch + post + pin standings in the summary channel")
@app_commands.default_permissions(manage_guild=True)
async def slash_forcestandings(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(embed=_error_embed("Use inside a server."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    summary_cid = cfg.get("summary_channel_id") or cfg.get("channel_id")
    if not summary_cid:
        await interaction.followup.send(
            embed=_error_embed("No summary channel set.", "Run /setsummarychannel first."), ephemeral=True
        )
        return
    ch = bot.get_channel(int(summary_cid))
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(embed=_error_embed("Summary channel not accessible."), ephemeral=True)
        return

    try:
        standings_data = await get_standings()
    except Exception as e:
        await interaction.followup.send(embed=_error_embed(f"API error: {e}"), ephemeral=True)
        return

    if not standings_data:
        await interaction.followup.send(embed=_error_embed("No standings data available yet."), ephemeral=True)
        return

    last_msg = None
    count = 0
    for table in (standings_data.get("standings") or []):
        if table.get("type") == "TOTAL":
            group  = table.get("group", "")
            letter = group.replace("GROUP_", "") if group else "?"
            rows   = table.get("table", [])
            last_msg = await ch.send(embed=emb.embed_wc_group(letter, rows))
            count += 1

    if last_msg:
        old_sid = state.get_pinned_standings(gid)
        if old_sid:
            try:
                old_smsg = await ch.fetch_message(int(old_sid))
                await old_smsg.unpin()
            except discord.HTTPException:
                pass
        try:
            await last_msg.pin()
            state.save_pinned_standings(gid, last_msg.id)
        except discord.HTTPException:
            pass

    await interaction.followup.send(
        embed=discord.Embed(
            title="📊 Standings Posted",
            description=f"✅ Posted **{count}** group table(s) to {ch.mention} and pinned the last one.",
            color=emb.C_GREEN,
        ),
        ephemeral=True,
    )


@tree.command(name="forceleaderboard", description="[Admin] Immediately post + pin the leaderboard in the predictions channel")
@app_commands.default_permissions(manage_guild=True)
async def slash_forceleaderboard(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(embed=_error_embed("Use inside a server."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    pred_cid = cfg.get("predictions_channel_id") or cfg.get("channel_id")
    if not pred_cid:
        await interaction.followup.send(
            embed=_error_embed("No predictions channel set.", "Run /setpredictionschannel first."), ephemeral=True
        )
        return
    lb = state.get_leaderboard(gid)
    if not lb:
        await interaction.followup.send(
            embed=_error_embed("Leaderboard is empty.", "No predictions scored yet."), ephemeral=True
        )
        return
    await _update_leaderboard_in_predictions(gid, cfg)
    ch = bot.get_channel(int(pred_cid))
    ch_mention = ch.mention if ch else f"`{pred_cid}`"
    await interaction.followup.send(
        embed=discord.Embed(
            title="🏅 Leaderboard Posted",
            description=f"✅ Leaderboard posted and pinned in {ch_mention}.",
            color=emb.C_GREEN,
        ),
        ephemeral=True,
    )


@tree.command(name="clearsentevents", description="[Admin] Clear sent-event flags for a match — allows re-testing broadcasts")
@app_commands.describe(match_id="Match ID to clear")
@app_commands.default_permissions(manage_guild=True)
async def slash_clearsentevents(interaction: discord.Interaction, match_id: int):
    if not interaction.guild:
        await interaction.response.send_message(embed=_error_embed("Use inside a server."), ephemeral=True)
        return
    mid = str(match_id)
    before = list(state.get_sent(mid))
    # Clear sent flags and reminder flags for this match
    state._state.get("sent", {}).pop(mid, None)
    state._state.get("reminders", {}).pop(mid, None)
    state.save()
    after = list(state.get_sent(mid))
    em = discord.Embed(
        title="🗑️ Sent Events Cleared",
        description=(
            f"Match ID `{match_id}`\n\n"
            f"**Before:** `{', '.join(before) or 'none'}`\n"
            f"**After:** `{', '.join(after) or 'none (cleared)'}`\n\n"
            "The monitor loop will re-fire events for this match on the next poll cycle."
        ),
        color=emb.C_GREEN,
    )
    await interaction.response.send_message(embed=em, ephemeral=True)


# ── /countdown ────────────────────────────────────────────────────────────────

_register("Info", "countdown", "Time remaining until the FIFA World Cup 2026 kicks off")

@tree.command(name="countdown", description="Time remaining until the FIFA World Cup 2026 kicks off")
async def slash_countdown(interaction: discord.Interaction):
    wc_start = datetime(2026, 6, 11, 20, 0, 0, tzinfo=timezone.utc)
    now      = datetime.now(timezone.utc)

    if now >= wc_start:
        delta  = now - wc_start
        days   = delta.days
        em = discord.Embed(
            title="🏆 The World Cup is UNDERWAY!",
            description=f"The 2026 FIFA World Cup kicked off **{days} day{'s' if days != 1 else ''} ago**!\nGo check `/live` or `/today` for matches right now.",
            colour=discord.Colour.gold(),
        )
    else:
        delta   = wc_start - now
        days    = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes          = remainder // 60
        em = discord.Embed(
            title="⏳ Countdown to FIFA World Cup 2026",
            description=(
                f"**{days}** days · **{hours}** hours · **{minutes}** minutes\n\n"
                "🏟️ USA · Canada · Mexico\n"
                "📅 Opening match: 11 June 2026 · 20:00 UTC"
            ),
            colour=discord.Colour.blurple(),
        )
        if days <= 7:
            em.description = "🔥 **Almost here!**\n\n" + (em.description or "")
        elif days <= 30:
            em.description = "🎉 **Less than a month to go!**\n\n" + (em.description or "")

    em.set_footer(text="🏆 FIFA World Cup 2026  •  48 teams  •  104 matches")
    await interaction.response.send_message(embed=em)


# ── Presence cycling task ──────────────────────────────────────────────────────

_PRESENCE_CYCLE: list[tuple[discord.ActivityType, str]] = [
    (discord.ActivityType.watching,  "⚽ World Cup 2026"),
    (discord.ActivityType.listening, "🏆 /help for commands"),
    (discord.ActivityType.watching,  "🔴 Live matches"),
    (discord.ActivityType.playing,   "📊 /standings"),
    (discord.ActivityType.watching,  "48 teams · 104 matches"),
]
_presence_idx = 0

@tasks.loop(minutes=5)
async def presence_loop() -> None:
    global _presence_idx
    act_type, text = _PRESENCE_CYCLE[_presence_idx % len(_PRESENCE_CYCLE)]
    await bot.change_presence(activity=discord.Activity(type=act_type, name=text))
    _presence_idx += 1

@presence_loop.before_loop
async def _before_presence() -> None:
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        log.info("[BOT] Shutting down…")
    finally:
        await close_session()


if __name__ == "__main__":
    asyncio.run(main())
