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
from datetime import datetime, timezone, timedelta
from typing import Any

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
    get_live_matches,
    get_match_detail,
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

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

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
    cfg  = state.get_guild_config(gid)
    mode = cfg.get("mode", "standard")
    return _MODE_RANK.get(mode, 1) >= _MODE_RANK.get(required, 0)


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

async def _send_to_channel(gid: str, cfg: dict, key: str, **kwargs) -> None:
    cid = cfg.get(key)
    if not cid:
        return
    ch = bot.get_channel(int(cid))
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
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
    ch = bot.get_channel(int(cid))
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
        try:
            return await ch.send(**kwargs)
        except discord.HTTPException as e:
            log.error("[BROADCAST] Failed: %s", e)
    return None


async def broadcast(embed: discord.Embed, min_mode: str = "quiet") -> None:
    coros = [
        _send_to_channel(gid, cfg, "channel_id", embed=embed)
        for gid, cfg in state.all_guild_configs().items()
        if _mode_at_least(gid, min_mode)
    ]
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def broadcast_predictions(embed: discord.Embed, view: discord.ui.View | None = None) -> None:
    coros = []
    for gid, cfg in state.all_guild_configs().items():
        if _mode_at_least(gid, "standard"):
            kwargs: dict[str, Any] = {"embed": embed}
            if view:
                kwargs["view"] = view
            coros.append(_send_to_channel(gid, cfg, "predictions_channel_id", **kwargs))
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def broadcast_results(embed: discord.Embed) -> None:
    coros = [
        _send_to_channel(gid, cfg, "results_channel_id", embed=embed)
        for gid, cfg in state.all_guild_configs().items()
    ]
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def broadcast_summary(embed: discord.Embed) -> None:
    coros = [
        _send_to_channel(gid, cfg, "summary_channel_id", embed=embed)
        for gid, cfg in state.all_guild_configs().items()
        if _mode_at_least(gid, "detailed")
    ]
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def _update_leaderboard_in_results(gid: str, cfg: dict) -> None:
    lb = state.get_leaderboard(gid)
    if not lb:
        return
    guild = bot.get_guild(int(gid))
    if not guild:
        return

    lb_embed = emb.embed_leaderboard(guild, lb)
    cid = cfg.get("results_channel_id")
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
            state.set_channel_id(gid, self.cfg_key, str(self.values[0].id))
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
        ("Live Alerts Channel",   "channel_id",             "📡 Select your live match notifications channel"),
        ("Predictions Channel",   "predictions_channel_id", "🎯 Select your predictions & MOTM voting channel"),
        ("Results Channel",       "results_channel_id",     "🏆 Select your prediction results channel"),
        ("Match Recaps Channel",  "summary_channel_id",     "📺 Select your match recaps & highlights channel"),
    ]

    def __init__(self, step: int = 0):
        super().__init__(timeout=300)
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

_monitor_tick: int = 0  # counts loop iterations for periodic tasks

@tasks.loop(seconds=60)
async def monitor_loop() -> None:
    global _monitor_tick
    _monitor_tick += 1

    # Refresh WC match order every 60 min (prevents stale/missing order at startup)
    if _monitor_tick % 60 == 1:
        try:
            await load_wc_match_order()
        except Exception as e:
            log.warning("[MONITOR] Match-order refresh failed: %s", e)

    configs = state.all_guild_configs()
    if not any(cfg.get("channel_id") for cfg in configs.values()):
        return

    try:
        live_matches = await get_live_matches()
    except Exception as e:
        log.error("[MONITOR] Failed to fetch live matches: %s", e)
        return

    try:
        all_wc = await get_competition_matches(
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        log.error("[MONITOR] Failed to fetch today's matches: %s", e)
        all_wc = []

    now = datetime.now(timezone.utc)

    # — Reminders for upcoming matches —
    for match in all_wc:
        mid = str(match["id"])
        dt  = parse_dt(match.get("utcDate", ""))
        if not dt:
            continue
        diff = (dt - now).total_seconds() / 60

        for minutes in (60, 15):
            if abs(diff - minutes) < 2.0 and not state.is_reminder_sent(mid, minutes):
                state.mark_reminder_sent(mid, minutes)
                log.info("[MONITOR] Reminder %d min for match %s", minutes, mid)
                await broadcast(emb.embed_reminder(_annotate_match(match), minutes), min_mode="standard")

        # Prediction poll — 90 min before kickoff
        if 88 < diff < 92 and not state.is_reminder_sent(mid, 90):
            state.mark_reminder_sent(mid, 90)
            home = team_display(match.get("homeTeam", {}))
            away = team_display(match.get("awayTeam", {}))
            ko   = is_knockout(match)
            em_pred = emb.embed_prediction_poll(_annotate_match(match))
            view = PredictionView(mid, home, away, ko)
            for gid, cfg in configs.items():
                if _mode_at_least(gid, "standard"):
                    await _send_to_channel(gid, cfg, "predictions_channel_id", embed=em_pred, view=view)
            log.info("[MONITOR] Prediction poll posted for match %s", mid)

    # — Process live matches —
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
                state.lock_predictions(mid)
                detail = await get_match_detail(match["id"])
                log.info("[MONITOR] Kick-off for match %s", mid)
                await broadcast(emb.embed_kickoff(_annotate_match(match), detail or {}), min_mode="quiet")

                if detail and has_confirmed_lineups(detail) and "LINEUP" not in sent:
                    state.mark_sent(mid, "LINEUP")
                    await broadcast(emb.embed_lineups(_annotate_match(match), detail), min_mode="detailed")

        await _process_live_match(match, snap, stat_changed)

        if stat_changed and cur_stat == "FINISHED" and "FT" not in state.get_sent(mid):
            state.mark_sent(mid, "FT")
            detail = await get_match_detail(match["id"])
            log.info("[MONITOR] Full time for match %s", mid)
            await _process_fulltime(match, detail)


async def _process_live_match(match: dict, snap: dict, status_changed: bool) -> None:
    mid    = str(match["id"])
    detail = await get_match_detail(match["id"])
    if not detail:
        return

    goals     = detail.get("goals") or []
    bookings  = detail.get("bookings") or []
    red_cards = [b for b in bookings if b.get("card") in ("RED", "YELLOW_RED")]
    cur_stat  = match.get("status", "")
    h, a      = get_current_score(match)
    ann_match = _annotate_match(match)

    # Goals
    announced = state.get_announced_goals(mid)
    for goal in goals:
        gk = goal_key(goal)
        if gk not in announced:
            state.announce_goal(mid, gk)
            scorer = (goal.get("scorer") or {}).get("name", "")
            log.info("[GOAL] Match %s — %s' %s", mid, goal.get("minute", "?"), scorer)
            await broadcast(emb.embed_goal(ann_match, detail, goal))

    # Red cards
    announced_c = state.get_announced_cards(mid)
    for card in red_cards:
        ck = card_key(card)
        if ck not in announced_c:
            state.announce_card(mid, ck)
            player = (card.get("player") or {}).get("name", "?")
            log.info("[CARD] Match %s — %s' %s", mid, card.get("minute", "?"), player)
            await broadcast(emb.embed_red_card(ann_match, detail, card))

    # Status transitions
    sent      = state.get_sent(mid)
    prev_stat = snap.get("status", "")

    if status_changed:
        if cur_stat == "PAUSED" and "HT" not in sent:
            state.mark_sent(mid, "HT")
            log.info("[MONITOR] Half-time for match %s", mid)
            await broadcast(emb.embed_halftime(ann_match, detail))

        elif cur_stat == "IN_PLAY" and prev_stat == "PAUSED" and "2H" not in sent:
            state.mark_sent(mid, "2H")
            log.info("[MONITOR] Second half for match %s", mid)
            await broadcast(emb.embed_second_half(ann_match), min_mode="detailed")

        elif cur_stat == "EXTRA_TIME" and "ET" not in sent:
            state.mark_sent(mid, "ET")
            log.info("[MONITOR] Extra time for match %s", mid)
            await broadcast(emb.embed_extra_time(ann_match, detail), min_mode="standard")

        elif cur_stat == "PENALTY_SHOOTOUT" and "PSO" not in sent:
            state.mark_sent(mid, "PSO")
            log.info("[MONITOR] Penalty shootout for match %s", mid)
            await broadcast(emb.embed_penalty_shootout(ann_match, detail), min_mode="standard")

    # MOTM vote — at ~75 min (outside status_changed so it fires even when status stays IN_PLAY)
    minute = match.get("minute") or 0
    if cur_stat == "IN_PLAY" and int(minute) >= 75 and "MOTM" not in sent:
        detail2  = await get_match_detail(match["id"])
        nominees = _build_motm_nominees(detail2 or {})
        if nominees:
            state.mark_sent(mid, "MOTM")
            motm_em   = emb.embed_motm_vote(ann_match)
            motm_view = MotmVoteView(mid, nominees)
            for gid, cfg in state.all_guild_configs().items():
                if _mode_at_least(gid, "standard"):
                    msg = await _send_to_channel_return(gid, cfg, "predictions_channel_id", embed=motm_em, view=motm_view)
                    if msg:
                        state.save_motm_message_id(mid, gid, msg.id)

    state.set_snapshot(mid, {"home_score": h, "away_score": a, "status": cur_stat})


def _build_motm_nominees(detail: dict) -> list[str]:
    goals    = detail.get("goals", [])
    lineups  = detail.get("lineups", [])
    nominees: dict[str, int] = {}

    for g in goals:
        scorer = (g.get("scorer") or {}).get("name")
        assist = (g.get("assist") or {}).get("name")
        if scorer:
            nominees[scorer] = nominees.get(scorer, 0) + 2
        if assist:
            nominees[assist] = nominees.get(assist, 0) + 1

    for lineup in lineups:
        for entry in lineup.get("startXI", []):
            p    = entry.get("player") or entry
            name = p.get("name")
            if name and name not in nominees:
                nominees[name] = 0

    return [n for n, _ in sorted(nominees.items(), key=lambda x: x[1], reverse=True)][:15]


async def _process_fulltime(match: dict, detail: dict | None) -> None:
    mid = str(match["id"])
    log.info("[MONITOR] Processing full time for match %s", mid)

    if detail is None:
        log.warning("[MONITOR] No detail for FT match %s — using basic data", mid)

    ann_match = _annotate_match(match)
    await broadcast(emb.embed_fulltime(ann_match, detail or match))

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

    # MOTM results
    for gid, cfg in state.all_guild_configs().items():
        motm_msg_id = state.get_motm_message_id(mid, gid)
        if not motm_msg_id:
            continue
        votes = state.get_motm_votes(mid, gid)
        if not votes:
            continue
        tally: dict[str, int] = {}
        for uid, player in votes.items():
            tally[player] = tally.get(player, 0) + 1
        if not tally:
            continue
        max_v   = max(tally.values())
        winners = [p for p, v in tally.items() if v == max_v]
        for uid, player in votes.items():
            if player in winners:
                state.update_leaderboard(gid, uid, 1)
        cid = cfg.get("results_channel_id")
        if cid:
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    await ch.send(embed=emb.embed_motm_result(ann_match, winners, tally))
                except discord.HTTPException:
                    pass
        # Lock MOTM voting message
        pred_cid = cfg.get("predictions_channel_id")
        if pred_cid and motm_msg_id:
            ch = bot.get_channel(int(pred_cid))
            if ch:
                try:
                    motm_msg = await ch.fetch_message(int(motm_msg_id))
                    if motm_msg.embeds:
                        locked_em = motm_msg.embeds[0].copy()
                        locked_em.set_footer(text="🔒 Voting closed  •  🏆 FIFA World Cup 2026")
                        await motm_msg.edit(embed=locked_em, view=None)
                except discord.HTTPException:
                    pass

    # Update leaderboards in results channels
    for gid, cfg in state.all_guild_configs().items():
        if state.get_leaderboard(gid):
            await _update_leaderboard_in_results(gid, cfg)

    asyncio.create_task(_post_delayed_recap(match, 3600))


async def _post_delayed_recap(match: dict, delay: int) -> None:
    await asyncio.sleep(delay)
    mid    = str(match["id"])
    log.info("[MONITOR] Posting recap for match %s", mid)
    detail = await get_match_detail(match["id"])
    home   = team_display(match.get("homeTeam", {}))
    away   = team_display(match.get("awayTeam", {}))
    query  = f"{home} vs {away} World Cup 2026"
    yt     = await search_youtube_highlights(query)
    await broadcast_summary(emb.embed_full_recap(_annotate_match(match), detail, yt))


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

    try:
        matches = await get_todays_matches()
    except Exception as e:
        log.error("[SUMMARY] Failed to fetch: %s", e)
        return

    for gid, cfg in configs.items():
        cid = cfg.get("channel_id")
        if not cid:
            continue
        tz        = _tz_for(gid)
        local_now = datetime.now(tz)
        if local_now.hour != 23 or local_now.minute < 50:
            continue
        date_key = f"{gid}_{local_now.strftime('%Y-%m-%d')}"
        if state.is_daily_summary_sent(date_key):
            continue
        ch = bot.get_channel(int(cid))
        if not ch:
            continue
        date_str = local_now.strftime("%d %B %Y")
        try:
            await ch.send(embed=emb.embed_daily_summary(_annotate_matches(matches), date_str))
            state.mark_daily_summary_sent(date_key)
            log.info("[SUMMARY] Daily summary sent to guild %s", gid)
        except discord.HTTPException as e:
            log.error("[SUMMARY] Failed for guild %s: %s", gid, e)


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


# ── /dashboard ────────────────────────────────────────────────────────────────

_register("Match Commands", "dashboard", "Post the interactive World Cup dashboard panel")

@tree.command(name="dashboard", description="Post the interactive World Cup dashboard panel")
@app_commands.default_permissions(manage_channels=True)
async def slash_dashboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

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

    try:
        msg = await interaction.channel.send(embed=emb.embed_dashboard(), view=DashboardView())
        state.save_interactive_panel_msg(gid, cid, msg.id)
        await interaction.followup.send(
            embed=_success_embed(f"Dashboard posted in {interaction.channel.mention}."),
            ephemeral=True,
        )
        log.info("[BOT] Dashboard posted for guild %s", gid)
    except discord.HTTPException as e:
        await interaction.followup.send(
            embed=_error_embed(
                f"Failed to post dashboard: {e}",
                "Check that I have Send Messages permission here."
            ),
            ephemeral=True,
        )


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


# ── /schedule (alias for /upcoming) ──────────────────────────────────────────

_register("Match Commands", "schedule", "World Cup match schedule (alias for /upcoming)")

@tree.command(name="schedule", description="World Cup match schedule — upcoming fixtures")
@app_commands.describe(days="Days ahead to search (1–30, default 7)")
async def slash_schedule(interaction: discord.Interaction, days: int = 7):
    await _safe_defer(interaction)
    days  = max(1, min(days, 30))
    today = datetime.now(timezone.utc)
    try:
        matches = await get_competition_matches(
            today.strftime("%Y-%m-%d"),
            (today + timedelta(days=days)).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        log.error("[CMD] /schedule error: %s", e)
        await interaction.followup.send(embed=_error_embed("Failed to load schedule."))
        return
    await interaction.followup.send(embed=emb.embed_upcoming(_annotate_matches(matches), days))


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
    ann    = _annotate_match(detail)
    num    = _match_num_str(match_id)
    title_prefix = f"Match {num} — " if num else ""
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


@tree.command(name="setresultschannel", description="Set this channel for prediction results and MOTM winners")
@app_commands.default_permissions(manage_channels=True)
async def slash_setresultschannel(interaction: discord.Interaction):
    state.set_channel_id(str(interaction.guild_id), "results_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(
        embed=_make_channel_set_embed(interaction.channel, "prediction results")
    )

_register("Admin", "setresultschannel", "Set the prediction results channel")


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
    state.set_channel_id(gid, "commands_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(
        embed=_make_channel_set_embed(interaction.channel, "notification mode picker")
    )
    await _post_commands_menu_if_needed()

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


@tree.command(name="setmode", description="Set guild notification verbosity")
@app_commands.describe(mode="quiet | standard | detailed")
@app_commands.choices(mode=[
    app_commands.Choice(name="🔇 Quiet — Goals + Red Cards + Full Time only",      value="quiet"),
    app_commands.Choice(name="📢 Standard — + HT, ET, MOTM Polls, Predictions",    value="standard"),
    app_commands.Choice(name="📋 Detailed — Everything: lineups, recaps, tables",  value="detailed"),
])
@app_commands.default_permissions(manage_channels=True)
async def slash_setmode(interaction: discord.Interaction, mode: str = "standard"):
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    cfg["mode"] = mode
    state.set_guild_config(gid, cfg)
    icons = {"quiet": "🔇", "standard": "📢", "detailed": "📋"}
    await interaction.response.send_message(
        embed=_success_embed(f"{icons.get(mode, '📢')} Notification mode set to **{mode}**.")
    )

_register("Admin", "setmode", "Set guild notification verbosity (quiet / standard / detailed)")


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


@tree.command(name="updatecommandsmenu", description="Rebuild and repost the notification mode picker")
@app_commands.default_permissions(manage_channels=True)
async def slash_updatecommandsmenu(interaction: discord.Interaction):
    await _safe_defer(interaction)
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    cid = cfg.get("commands_channel_id")
    if not cid:
        await interaction.followup.send(
            embed=_error_embed(
                "No commands channel configured.",
                "Run `/setcommandschannel` in the channel you want to use first."
            )
        )
        return
    ch = bot.get_channel(int(cid))
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(
            embed=_error_embed(
                "Commands channel not found — it may have been deleted.",
                "Run `/setcommandschannel` again."
            )
        )
        return
    old_id = state.get_commands_menu_message(gid)
    if old_id:
        try:
            old = await ch.fetch_message(int(old_id))
            await old.delete()
        except discord.HTTPException:
            pass
    try:
        msg = await ch.send(embed=emb.embed_commands_menu(), view=ModePicker())
        state.save_commands_menu_message(gid, msg.id)
        await interaction.followup.send(embed=_success_embed("Commands menu rebuilt and reposted."))
    except discord.HTTPException as e:
        await interaction.followup.send(embed=_error_embed(f"Failed: {e}"))

_register("Admin", "updatecommandsmenu", "Rebuild and repost the notification mode picker")


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
            "Live Alerts":   cfg.get("channel_id"),
            "Predictions":   cfg.get("predictions_channel_id"),
            "Results":       cfg.get("results_channel_id"),
            "Recaps":        cfg.get("summary_channel_id"),
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
        has_results = bool(cfg.get("results_channel_id"))
        results.append(("This Guild: live channel",    "✅" if has_live    else "⚠️", "Set" if has_live    else "Not configured — run /setmatcheschannel"))
        results.append(("This Guild: pred channel",    "✅" if has_pred    else "⚠️", "Set" if has_pred    else "Not configured — run /setpredictionschannel"))
        results.append(("This Guild: results channel", "✅" if has_results else "⚠️", "Set" if has_results else "Not configured — run /setresultschannel"))

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


# ── /debug ─────────────────────────────────────────────────────────────────────

_register("Admin", "debug", "Debug state for a specific match ID")

@tree.command(name="debug", description="[Admin] Debug internal state for a specific match")
@app_commands.describe(match_id="Match ID to inspect (0 = show general bot state)")
@app_commands.default_permissions(administrator=True)
async def slash_debug(interaction: discord.Interaction, match_id: int = 0):
    await interaction.response.defer(ephemeral=True)

    em = discord.Embed(title="🐛 Debug Info", color=emb.C_BLUE)

    if match_id == 0:
        # General state
        order = get_wc_match_order()
        configs = state.all_guild_configs()
        em.add_field(name="Match Cache Size", value=str(len(order)), inline=True)
        em.add_field(name="Configured Guilds", value=str(len(configs)), inline=True)
        em.add_field(name="Monitor Running", value=str(monitor_loop.is_running()), inline=True)
        em.add_field(name="Summary Running", value=str(daily_summary_loop.is_running()), inline=True)
        em.add_field(name="Bot Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
        em.add_field(name="Guild Count", value=str(len(bot.guilds)), inline=True)

        if interaction.guild:
            gid = str(interaction.guild_id)
            cfg = state.get_guild_config(gid)
            em.add_field(
                name="Guild Config",
                value=f"```\nmode: {cfg.get('mode', 'standard')}\ntz: {cfg.get('timezone', 'UTC')}\nlive_ch: {cfg.get('channel_id', 'none')}\npred_ch: {cfg.get('predictions_channel_id', 'none')}\nresults_ch: {cfg.get('results_channel_id', 'none')}\n```",
                inline=False,
            )
    else:
        mid = str(match_id)
        snap   = state.get_snapshot(mid)
        sent   = state.get_sent(mid)
        goals  = state.get_announced_goals(mid)
        cards  = state.get_announced_cards(mid)
        preds  = state.get_score_predictions(mid)
        num    = _match_num_str(match_id)

        em.add_field(name="Match ID", value=f"{match_id} {num}", inline=True)
        em.add_field(name="Snapshot", value=str(snap) or "none", inline=False)
        em.add_field(name="Events Sent", value=", ".join(sent) if sent else "none", inline=True)
        em.add_field(name="Announced Goals", value=str(len(goals)), inline=True)
        em.add_field(name="Announced Cards", value=str(len(cards)), inline=True)
        em.add_field(name="Score Predictions", value=str(len(preds)), inline=True)

        if interaction.guild:
            gid  = str(interaction.guild_id)
            votes = state.get_motm_votes(mid, gid)
            em.add_field(name="MOTM Votes (this guild)", value=str(len(votes)), inline=True)

    em.set_footer(text=f"🏆 FIFA World Cup 2026  •  debug at {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    await interaction.followup.send(embed=em, ephemeral=True)


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


# ── /testnotifications ─────────────────────────────────────────────────────────

_register("Admin", "testnotifications", "Send a test notification to configured channels")

_TEST_NOTIF_TYPES = (
    "kickoff", "goal", "redcard", "halftime", "fulltime",
    "reminder15", "extratime", "pso", "lineup", "recap", "daily",
)

@tree.command(name="testnotifications", description="[Admin] Send a test notification embed to configured channels")
@app_commands.describe(notification_type="Which notification type to test-broadcast")
@app_commands.choices(notification_type=[app_commands.Choice(name=t, value=t) for t in _TEST_NOTIF_TYPES])
@app_commands.default_permissions(administrator=True)
async def slash_testnotifications(interaction: discord.Interaction, notification_type: str = "goal"):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild:
        await interaction.followup.send(embed=_error_embed("Use this inside a server."), ephemeral=True)
        return

    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    ch_id = cfg.get("channel_id")

    if not ch_id:
        await interaction.followup.send(
            embed=_error_embed(
                "No live alerts channel configured for this server.",
                "Run `/setmatcheschannel` first."
            ),
            ephemeral=True,
        )
        return

    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(
            embed=_error_embed("Live alerts channel not found — it may have been deleted.", "Run `/setmatcheschannel` again."),
            ephemeral=True,
        )
        return

    try:
        test_embed = emb.embed_test(notification_type)
        await ch.send(embed=test_embed)
        await interaction.followup.send(
            embed=_success_embed(
                f"Test `{notification_type}` notification sent to {ch.mention}.\n"
                "Check the channel to confirm it looks correct."
            ),
            ephemeral=True,
        )
        log.info("[TEST] Notification type '%s' sent to guild %s by %s", notification_type, gid, interaction.user)
    except discord.Forbidden:
        await interaction.followup.send(
            embed=_error_embed(
                "I don't have permission to send messages in that channel.",
                "Check my channel permissions and try again."
            ),
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(
            embed=_error_embed(f"Failed to send test notification: {e}"),
            ephemeral=True,
        )


# ── /testpredictions ──────────────────────────────────────────────────────────

_register("Admin", "testpredictions", "Test the prediction system end-to-end")

_TEST_MATCH_ID_PRED  = "test-predict-0001"
_TEST_HOME_TEAM_PRED = "Brazil"
_TEST_AWAY_TEAM_PRED = "Argentina"

@tree.command(name="testpredictions", description="[Admin] Test the prediction system — open, submit, view, resolve")
@app_commands.describe(
    action="What step of the prediction workflow to test",
    home_score="Actual home score for resolving (use with action=resolve)",
    away_score="Actual away score for resolving (use with action=resolve)",
)
@app_commands.choices(action=[
    app_commands.Choice(name="open — post a test prediction poll",           value="open"),
    app_commands.Choice(name="status — show current test predictions",       value="status"),
    app_commands.Choice(name="resolve — score predictions & award points",   value="resolve"),
    app_commands.Choice(name="clear — wipe all test prediction data",        value="clear"),
])
@app_commands.default_permissions(administrator=True)
async def slash_testpredictions(
    interaction: discord.Interaction,
    action: str = "open",
    home_score: int = 1,
    away_score: int = 0,
):
    if not interaction.guild:
        await interaction.response.send_message(embed=_error_embed("Use this inside a server."), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    mid = _TEST_MATCH_ID_PRED

    if action == "open":
        # Build a fake match dict
        fake_match = {
            "id":       mid,
            "homeTeam": {"name": _TEST_HOME_TEAM_PRED, "shortName": _TEST_HOME_TEAM_PRED},
            "awayTeam": {"name": _TEST_AWAY_TEAM_PRED, "shortName": _TEST_AWAY_TEAM_PRED},
            "utcDate":  datetime.now(timezone.utc).isoformat(),
            "stage":    "GROUP_STAGE",
            "status":   "SCHEDULED",
            "matchNumber": None,
        }
        em_pred = emb.embed_prediction_poll(fake_match)
        view    = PredictionView(mid, _TEST_HOME_TEAM_PRED, _TEST_AWAY_TEAM_PRED, knockout=False)

        # Try to post to predictions channel; fall back to current channel
        pred_cid = cfg.get("predictions_channel_id")
        target_ch = bot.get_channel(int(pred_cid)) if pred_cid else interaction.channel

        try:
            await target_ch.send(
                content="🧪 **Admin test prediction poll** — predictions will be scored when you use `/testpredictions resolve`",
                embed=em_pred,
                view=view,
            )
            await interaction.followup.send(
                embed=_success_embed(
                    f"Test prediction poll posted in {target_ch.mention}.\n"
                    f"Match ID: `{mid}`\n"
                    "Users can now make predictions. Use `action=status` to check, or `action=resolve` to score them."
                ),
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.followup.send(embed=_error_embed(f"Failed to post poll: {e}"), ephemeral=True)

    elif action == "status":
        preds = state.get_score_predictions(mid)
        locked = state.is_predictions_locked(mid) if hasattr(state, "is_predictions_locked") else "unknown"
        em = discord.Embed(title="🧪 Test Prediction Status", color=emb.C_BLUE)
        em.add_field(name="Match ID", value=f"`{mid}`", inline=True)
        em.add_field(name="Predictions Submitted", value=str(len(preds)), inline=True)
        em.add_field(name="Predictions Locked", value=str(locked), inline=True)
        if preds:
            lines = [
                f"<@{uid}>: {p['home']}–{p['away']}"
                for uid, p in list(preds.items())[:10]
            ]
            em.add_field(name="Submitted Predictions (up to 10)", value="\n".join(lines), inline=False)
        em.add_field(
            name="Next Steps",
            value=(
                "• Use `action=resolve home_score=X away_score=Y` to score predictions\n"
                "• Use `action=clear` to wipe test data"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=em, ephemeral=True)

    elif action == "resolve":
        preds = state.get_score_predictions(mid)
        if not preds:
            await interaction.followup.send(
                embed=_error_embed(
                    "No predictions found for the test match.",
                    "Use `action=open` to post a poll first, then have users submit predictions."
                ),
                ephemeral=True,
            )
            return

        exact_winners  = []
        result_winners = []
        h, a = home_score, away_score

        for uid, pred in preds.items():
            ph, pa = pred["home"], pred["away"]
            if ph == h and pa == a:
                exact_winners.append((uid, ph, pa))
            elif _same_result(ph, pa, h, a):
                result_winners.append((uid, ph, pa))

        for uid, _, _ in exact_winners:
            state.update_leaderboard(gid, uid, 3)
        for uid, _, _ in result_winners:
            state.update_leaderboard(gid, uid, 1)

        fake_match = {
            "id":       mid,
            "homeTeam": {"name": _TEST_HOME_TEAM_PRED, "shortName": _TEST_HOME_TEAM_PRED},
            "awayTeam": {"name": _TEST_AWAY_TEAM_PRED, "shortName": _TEST_AWAY_TEAM_PRED},
            "stage":    "GROUP_STAGE",
            "matchNumber": None,
        }

        results_em = emb.embed_prediction_results(fake_match, h, a, exact_winners, result_winners)
        results_cid = cfg.get("results_channel_id") or cfg.get("channel_id")
        if results_cid:
            rch = bot.get_channel(int(results_cid))
            if rch:
                try:
                    await rch.send(content="🧪 **Test prediction results:**", embed=results_em)
                except discord.HTTPException:
                    pass

        await interaction.followup.send(
            embed=_success_embed(
                f"Test predictions resolved: **{h}–{a}**\n"
                f"• {len(exact_winners)} exact score(s) → +3 pts each\n"
                f"• {len(result_winners)} correct result(s) → +1 pt each\n"
                f"Results posted to results channel (if configured)."
            ),
            ephemeral=True,
        )

    elif action == "clear":
        # Clear test prediction state
        try:
            state.clear_match_state(mid)
        except AttributeError:
            # Fallback: just log; the state module may expose a different API
            log.warning("[TEST] state.clear_match_state not available — test data not cleared")
        await interaction.followup.send(
            embed=_success_embed(f"Test prediction data cleared for match `{mid}`."),
            ephemeral=True,
        )


# ── /testmotm ─────────────────────────────────────────────────────────────────

_register("Admin", "testmotm", "Test the MOTM voting system without a live match")

_TEST_MATCH_ID_MOTM = "test-motm-0001"
_TEST_MOTM_NOMINEES = [
    "Lionel Messi", "Kylian Mbappé", "Vinícius Júnior",
    "Erling Haaland", "Jude Bellingham", "Rodri",
    "Bukayo Saka", "Phil Foden",
]

@tree.command(name="testmotm", description="[Admin] Test MOTM voting workflow without a live match")
@app_commands.describe(action="open = post vote, results = show winner and award points, clear = reset")
@app_commands.choices(action=[
    app_commands.Choice(name="open — post a test MOTM vote",       value="open"),
    app_commands.Choice(name="results — tally votes, award points", value="results"),
    app_commands.Choice(name="status — view current votes",         value="status"),
    app_commands.Choice(name="clear — wipe test MOTM data",         value="clear"),
])
@app_commands.default_permissions(administrator=True)
async def slash_testmotm(
    interaction: discord.Interaction,
    action: str = "open",
):
    if not interaction.guild:
        await interaction.response.send_message(embed=_error_embed("Use this inside a server."), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    mid = _TEST_MATCH_ID_MOTM

    fake_match = {
        "id":       mid,
        "homeTeam": {"name": "Brazil",    "shortName": "Brazil"},
        "awayTeam": {"name": "Argentina", "shortName": "Argentina"},
        "stage":    "GROUP_STAGE",
        "status":   "IN_PLAY",
        "matchNumber": None,
    }

    if action == "open":
        motm_em   = emb.embed_motm_vote(fake_match)
        motm_view = MotmVoteView(mid, _TEST_MOTM_NOMINEES)

        pred_cid  = cfg.get("predictions_channel_id")
        target_ch = bot.get_channel(int(pred_cid)) if pred_cid else interaction.channel

        try:
            msg = await target_ch.send(
                content="🧪 **Admin MOTM test vote** — use `/testmotm action=results` to close and tally",
                embed=motm_em,
                view=motm_view,
            )
            state.save_motm_message_id(mid, gid, msg.id)
            await interaction.followup.send(
                embed=_success_embed(
                    f"Test MOTM vote posted in {target_ch.mention}.\n"
                    f"Nominees: {', '.join(_TEST_MOTM_NOMINEES[:4])}…\n"
                    "Use `action=results` to close voting and award points."
                ),
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.followup.send(embed=_error_embed(f"Failed to post MOTM vote: {e}"), ephemeral=True)

    elif action == "status":
        votes = state.get_motm_votes(mid, gid)
        em = discord.Embed(title="🧪 Test MOTM Vote Status", color=emb.C_BLUE)
        em.add_field(name="Total Votes", value=str(len(votes)), inline=True)
        if votes:
            tally: dict[str, int] = {}
            for _, player in votes.items():
                tally[player] = tally.get(player, 0) + 1
            lines = [f"**{p}**: {v} vote(s)" for p, v in sorted(tally.items(), key=lambda x: x[1], reverse=True)]
            em.add_field(name="Current Tally", value="\n".join(lines[:10]) or "none", inline=False)
        else:
            em.description = "No votes submitted yet. Make sure the poll is open and users have voted."
        await interaction.followup.send(embed=em, ephemeral=True)

    elif action == "results":
        votes = state.get_motm_votes(mid, gid)
        if not votes:
            await interaction.followup.send(
                embed=_error_embed(
                    "No MOTM votes found.",
                    "Use `action=open` to post the poll and collect votes first."
                ),
                ephemeral=True,
            )
            return

        tally: dict[str, int] = {}
        for _, player in votes.items():
            tally[player] = tally.get(player, 0) + 1
        max_v   = max(tally.values())
        winners = [p for p, v in tally.items() if v == max_v]

        # Award points
        for uid, player in votes.items():
            if player in winners:
                state.update_leaderboard(gid, uid, 1)

        results_em = emb.embed_motm_result(fake_match, winners, tally)
        results_cid = cfg.get("results_channel_id") or cfg.get("channel_id")
        if results_cid:
            rch = bot.get_channel(int(results_cid))
            if rch:
                try:
                    await rch.send(content="🧪 **Test MOTM results:**", embed=results_em)
                except discord.HTTPException:
                    pass

        # Lock the vote message
        motm_msg_id = state.get_motm_message_id(mid, gid)
        pred_cid    = cfg.get("predictions_channel_id")
        if pred_cid and motm_msg_id:
            ch = bot.get_channel(int(pred_cid))
            if ch:
                try:
                    motm_msg = await ch.fetch_message(int(motm_msg_id))
                    if motm_msg.embeds:
                        locked_em = motm_msg.embeds[0].copy()
                        locked_em.set_footer(text="🔒 Voting closed (test)  •  🏆 FIFA World Cup 2026")
                        await motm_msg.edit(embed=locked_em, view=None)
                except discord.HTTPException:
                    pass

        winner_str = " & ".join(winners)
        await interaction.followup.send(
            embed=_success_embed(
                f"MOTM test resolved!\n"
                f"**Winner(s):** {winner_str}\n"
                f"**{len([v for v in votes.values() if v in winners])}** voter(s) earned +1 pt.\n"
                "Results posted to results channel (if configured)."
            ),
            ephemeral=True,
        )

    elif action == "clear":
        try:
            state.clear_match_state(mid)
        except AttributeError:
            log.warning("[TEST] state.clear_match_state not available — MOTM test data not cleared")
        await interaction.followup.send(
            embed=_success_embed(f"MOTM test data cleared for match `{mid}`."),
            ephemeral=True,
        )


# ── /testembed ────────────────────────────────────────────────────────────────

_TEST_TYPES = (
    "reminder15", "kickoff", "lineup",
    "goal", "redcard",
    "halftime", "secondhalf", "extratime", "pso",
    "fulltime", "motm_vote", "motm_result",
    "prediction", "predresults",
    "recap", "daily", "leaderboard", "predstats",
)

_TEST_MATCH_ID  = "test-0000"
_TEST_HOME_TEAM = "Brazil"
_TEST_AWAY_TEAM = "Argentina"

_register("Admin", "testembed", "Preview any notification embed with realistic test data")

@tree.command(name="testembed", description="[Admin] Preview any notification embed with realistic test data")
@app_commands.describe(embed_type="Which notification type to preview")
@app_commands.choices(embed_type=[app_commands.Choice(name=t, value=t) for t in _TEST_TYPES])
@app_commands.default_permissions(administrator=True)
async def slash_testembed(interaction: discord.Interaction, embed_type: str):
    if embed_type == "prediction":
        view = PredictionView(_TEST_MATCH_ID, _TEST_HOME_TEAM, _TEST_AWAY_TEAM, knockout=False)
        await interaction.response.send_message(
            embed=emb.embed_test(embed_type), view=view, ephemeral=True
        )
    elif embed_type == "predstats":
        fake_stats = {
            "points": 14, "exact": 3, "correct": 8, "total_predictions": 12,
            "streak": 4, "best_streak": 6,
            "monthly": {"2026-06": {"points": 14, "exact": 3, "correct": 8}},
        }
        await interaction.response.send_message(
            embed=emb.embed_prediction_stats(interaction.user, fake_stats, rank=3),
            ephemeral=True,
        )
    elif embed_type == "leaderboard":
        fake_lb = {
            "111111111": {"points": 21, "exact": 5, "correct": 11, "streak": 6, "best_streak": 6, "total_predictions": 18, "monthly": {}},
            "222222222": {"points": 14, "exact": 3, "correct": 8,  "streak": 4, "best_streak": 5, "total_predictions": 12, "monthly": {}},
            str(interaction.user.id): {"points": 9, "exact": 1, "correct": 6, "streak": 1, "best_streak": 3, "total_predictions": 9, "monthly": {}},
        }
        await interaction.response.send_message(
            embed=emb.embed_leaderboard(interaction.guild, fake_lb), ephemeral=True
        )
    else:
        await interaction.response.send_message(
            embed=emb.embed_test(embed_type), ephemeral=True
        )


@bot.command(name="testembed", help="[Admin] Preview a notification embed. Usage: !testembed goal")
@commands.has_permissions(administrator=True)
async def prefix_testembed(ctx, embed_type: str = "goal"):
    if embed_type not in _TEST_TYPES:
        await ctx.send(embed=_error_embed(
            f"Unknown type `{embed_type}`.",
            "Valid types: " + ", ".join(f"`{t}`" for t in _TEST_TYPES),
        ))
        return
    if embed_type == "prediction":
        view = PredictionView(_TEST_MATCH_ID, _TEST_HOME_TEAM, _TEST_AWAY_TEAM, knockout=False)
        await ctx.send(embed=emb.embed_test(embed_type), view=view)
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


@bot.command(name="setmode")
@commands.has_permissions(manage_channels=True)
async def prefix_setmode(ctx, mode: str = "standard"):
    if mode not in ("quiet", "standard", "detailed"):
        await ctx.send(embed=_error_embed("Mode must be `quiet`, `standard`, or `detailed`."))
        return
    gid = str(ctx.guild.id)
    cfg = state.get_guild_config(gid)
    cfg["mode"] = mode
    state.set_guild_config(gid, cfg)
    icons = {"quiet": "🔇", "standard": "📢", "detailed": "📋"}
    await ctx.send(embed=_success_embed(f"{icons.get(mode, '📢')} Mode set to **{mode}**."))


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
