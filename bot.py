"""
bot.py — FIFA World Cup 2026 companion bot.
Production-quality: auto-help, dashboard panel, setup wizard,
autocomplete, advanced prediction stats, graceful error handling.
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
    parse_dt,
    search_team,
    search_youtube_highlights,
    team_display,
    team_flag,
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
# Every slash command auto-registers here; /help reads this at runtime.
# Format: {category: [{name, description}]}

COMMAND_REGISTRY: dict[str, list[dict]] = {
    "Match Commands": [],
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
#  UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _tz_for(gid: Any) -> ZoneInfo:
    cfg    = state.get_guild_config(str(gid))
    tz_str = cfg.get("timezone", "UTC")
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


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
        chunk = embeds[i:i + 10]
        if i == 0:
            await interaction.followup.send(embeds=chunk)
        else:
            await interaction.followup.send(embeds=chunk)


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


async def broadcast(embed: discord.Embed, min_mode: str = "quiet") -> None:
    for gid, cfg in state.all_guild_configs().items():
        if _mode_at_least(gid, min_mode):
            await _send_to_channel(gid, cfg, "channel_id", embed=embed)


async def broadcast_predictions(embed: discord.Embed, view: discord.ui.View | None = None) -> None:
    for gid, cfg in state.all_guild_configs().items():
        if _mode_at_least(gid, "standard"):
            kwargs = {"embed": embed}
            if view:
                kwargs["view"] = view
            await _send_to_channel(gid, cfg, "predictions_channel_id", **kwargs)


async def broadcast_results(embed: discord.Embed) -> None:
    for gid, cfg in state.all_guild_configs().items():
        await _send_to_channel(gid, cfg, "results_channel_id", embed=embed)


async def broadcast_summary(embed: discord.Embed) -> None:
    for gid, cfg in state.all_guild_configs().items():
        if _mode_at_least(gid, "detailed"):
            await _send_to_channel(gid, cfg, "summary_channel_id", embed=embed)


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
    return [app_commands.Choice(name=t, value=t) for t in matches]


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
    return [app_commands.Choice(name=t, value=t) for t in matches]


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
        self.knockout   = knockout
        self.home_score = 0
        self.away_score = 0
        self.changed    = False
        self._sync()

    def _is_draw(self) -> bool:
        return self.knockout and self.home_score == self.away_score

    def _sync(self) -> None:
        draw = self._is_draw()
        self.home_label_btn.label    = f"{self.home_name}  {self.home_score}"
        self.away_label_btn.label    = f"{self.away_name}  {self.away_score}"
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
            f"🏠 **{self.home_name}**  {self.home_score} – {self.away_score}  **{self.away_name}** ✈️"
            f"{ko}"
        )

    @discord.ui.button(label="−", style=discord.ButtonStyle.secondary, row=0)
    async def home_minus_btn(self, i: discord.Interaction, b: discord.ui.Button):
        self.home_score = max(0, self.home_score - 1); self.changed = True; self._sync()
        await i.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="Home 0", style=discord.ButtonStyle.primary, row=0, disabled=True)
    async def home_label_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer()

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=0)
    async def home_plus_btn(self, i: discord.Interaction, b: discord.ui.Button):
        self.home_score = min(9, self.home_score + 1); self.changed = True; self._sync()
        await i.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="−", style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def away_minus_btn(self, i: discord.Interaction, b: discord.ui.Button):
        self.away_score = max(0, self.away_score - 1); self.changed = True; self._sync()
        await i.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="Away 0", style=discord.ButtonStyle.primary, row=1, disabled=True)
    async def away_label_btn(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer()

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=1)
    async def away_plus_btn(self, i: discord.Interaction, b: discord.ui.Button):
        self.away_score = min(9, self.away_score + 1); self.changed = True; self._sync()
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

    @discord.ui.button(label="🔇 Quiet",    style=discord.ButtonStyle.secondary, custom_id="mp_quiet")
    async def quiet(self, i: discord.Interaction, b: discord.ui.Button):
        state.set_user_mode(str(i.guild_id), str(i.user.id), "quiet")
        await i.response.send_message(
            "🔇 **Quiet mode** — Goals + Red Cards + Full Time only.", ephemeral=True
        )

    @discord.ui.button(label="📢 Standard", style=discord.ButtonStyle.primary,   custom_id="mp_standard")
    async def standard(self, i: discord.Interaction, b: discord.ui.Button):
        state.set_user_mode(str(i.guild_id), str(i.user.id), "standard")
        await i.response.send_message(
            "📢 **Standard mode** — Goals, Cards, HT, FT, ET, MOTM Polls.", ephemeral=True
        )

    @discord.ui.button(label="📋 Detailed", style=discord.ButtonStyle.success,   custom_id="mp_detailed")
    async def detailed(self, i: discord.Interaction, b: discord.ui.Button):
        state.set_user_mode(str(i.guild_id), str(i.user.id), "detailed")
        await i.response.send_message(
            "📋 **Detailed mode** — Lineups, all events, recaps, group tables.", ephemeral=True
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


class UserSettingsView(discord.ui.View):
    def __init__(self, uid: str, gid: str):
        super().__init__(timeout=300)
        self.uid = uid
        self.gid = gid
        self.add_item(UserSettingsSelect(uid, gid))

    @discord.ui.button(label="🔄 Reset to Defaults", style=discord.ButtonStyle.danger, row=1)
    async def reset_btn(self, i: discord.Interaction, b: discord.ui.Button):
        state.set_user_prefs(self.uid, self.gid, {})
        new_view = UserSettingsView(self.uid, self.gid)
        await i.response.edit_message(
            embed=_build_settings_embed(self.uid, self.gid), view=new_view
        )


def _build_settings_embed(uid: str, gid: str) -> discord.Embed:
    prefs = state.get_user_prefs(uid, gid)
    em = discord.Embed(
        title="🔔 My Notification Settings",
        description=(
            "Toggle which World Cup notifications you receive.\n"
            "**ON** = receive  |  **OFF** = skip\n"
            "*All ON = follow the server's default mode.*"
        ),
        color=emb.C_BLUE,
    )
    rows = [
        f"{'✅' if prefs.get(k, True) else '❌'}  {label}"
        for k, label in _USER_SETTING_LABELS.items()
    ]
    em.add_field(name="Settings", value="\n".join(rows), inline=False)
    em.set_footer(text="Changes apply immediately • Only visible to you")
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
    """Persistent interactive dashboard — 7 buttons, all ephemeral responses."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚽ Live Matches",  style=discord.ButtonStyle.danger,     custom_id="dash_live",   row=0)
    async def dash_live(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        matches = await get_live_matches()
        await i.followup.send(embed=emb.embed_live(matches), ephemeral=True)

    @discord.ui.button(label="📅 Schedule",       style=discord.ButtonStyle.secondary,  custom_id="dash_sched",  row=0)
    async def dash_sched(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        matches = await get_todays_matches()
        embeds  = emb.embed_today(matches)
        await i.followup.send(embed=embeds[0], ephemeral=True)

    @discord.ui.button(label="🏆 Standings",      style=discord.ButtonStyle.primary,    custom_id="dash_stand",  row=0)
    async def dash_stand(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        data = await get_standings()
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


# ── Interactive panel (legacy / simpler version) ──────────────────────────────

class InteractivePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔔 My Notifications", style=discord.ButtonStyle.primary,   custom_id="ip_notif",  row=0)
    async def btn_notif(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.guild:
            await i.response.send_message("Use this in a server.", ephemeral=True); return
        uid = str(i.user.id); gid = str(i.guild_id)
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
            await i.response.send_message("Use this in a server.", ephemeral=True); return
        gid = str(i.guild_id)
        lb  = state.get_leaderboard(gid)
        await i.response.send_message(embed=emb.embed_leaderboard(i.guild, lb), ephemeral=True)

    @discord.ui.button(label="📅 Today's Matches",   style=discord.ButtonStyle.secondary, custom_id="ip_today",  row=1)
    async def btn_today(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        try:
            matches = await get_todays_matches()
            embeds  = emb.embed_today(matches)
            await i.followup.send(embed=embeds[0], ephemeral=True)
        except Exception as e:
            log.error("[PANEL] Today error: %s", e)
            await i.followup.send(
                embed=_error_embed("Could not load today's matches.", "Try `/today` instead."),
                ephemeral=True,
            )

    @discord.ui.button(label="📊 My Stats",          style=discord.ButtonStyle.secondary, custom_id="ip_stats",  row=1)
    async def btn_stats(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.guild:
            await i.response.send_message("Use this in a server.", ephemeral=True); return
        uid   = str(i.user.id); gid = str(i.guild_id)
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
    """Generic channel picker step in the setup wizard."""

    def __init__(self, cfg_key: str, label: str, placeholder: str):
        self.cfg_key = cfg_key
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
        ("Live Alerts Channel",       "channel_id",             "📡 Select your live match notifications channel"),
        ("Predictions Channel",       "predictions_channel_id", "🎯 Select your predictions & MOTM voting channel"),
        ("Results Channel",           "results_channel_id",     "🏆 Select your prediction results channel"),
        ("Match Recaps Channel",      "summary_channel_id",     "📺 Select your match recaps & highlights channel"),
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
                    discord.SelectOption(label="UTC",                    value="UTC"),
                    discord.SelectOption(label="US Eastern (UTC-5/-4)",  value="America/New_York"),
                    discord.SelectOption(label="US Central (UTC-6/-5)",  value="America/Chicago"),
                    discord.SelectOption(label="US Pacific (UTC-8/-7)",  value="America/Los_Angeles"),
                    discord.SelectOption(label="UK / Ireland (UTC+0/+1)",value="Europe/London"),
                    discord.SelectOption(label="Central Europe (UTC+1/+2)", value="Europe/Berlin"),
                    discord.SelectOption(label="Brazil (UTC-3)",         value="America/Sao_Paulo"),
                    discord.SelectOption(label="Mexico City (UTC-6/-5)", value="America/Mexico_City"),
                    discord.SelectOption(label="Argentina (UTC-3)",      value="America/Argentina/Buenos_Aires"),
                    discord.SelectOption(label="Japan (UTC+9)",          value="Asia/Tokyo"),
                    discord.SelectOption(label="Australia / Sydney",     value="Australia/Sydney"),
                    discord.SelectOption(label="India (UTC+5:30)",       value="Asia/Kolkata"),
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
                    discord.SelectOption(label="🔇 Quiet — Goals + Red Cards + Full Time only",         value="quiet"),
                    discord.SelectOption(label="📢 Standard — + HT, ET, MOTM Polls, Predictions",       value="standard"),
                    discord.SelectOption(label="📋 Detailed — Everything: Lineups, Recaps, Tables",      value="detailed"),
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
        total = len(self.STEPS) + 2  # channel steps + tz + mode
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

@tasks.loop(minutes=2)
async def monitor_loop() -> None:
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
            if abs(diff - minutes) < 1.5 and not state.is_reminder_sent(mid, minutes):
                state.mark_reminder_sent(mid, minutes)
                log.info("[MONITOR] Reminder %d min for match %s", minutes, mid)
                await broadcast(emb.embed_reminder(match, minutes), min_mode="standard")

        # Prediction poll — 90 min before kickoff
        if 88 < diff < 92 and not state.is_reminder_sent(mid, 90):
            state.mark_reminder_sent(mid, 90)
            home = team_display(match.get("homeTeam", {}))
            away = team_display(match.get("awayTeam", {}))
            ko   = is_knockout(match)
            em   = emb.embed_prediction_poll(match)
            view = PredictionView(mid, home, away, ko)
            for gid, cfg in configs.items():
                if _mode_at_least(gid, "standard"):
                    await _send_to_channel(gid, cfg, "predictions_channel_id", embed=em, view=view)
                    # Also save poll message
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
                await broadcast(emb.embed_kickoff(match, detail or {}), min_mode="quiet")

                # Lineups at kickoff
                if detail and has_confirmed_lineups(detail) and "LINEUP" not in sent:
                    state.mark_sent(mid, "LINEUP")
                    await broadcast(emb.embed_lineups(match, detail), min_mode="detailed")

        await _process_live_match(match, snap, stat_changed)

        # Check for full time
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

    # Goals
    announced = state.get_announced_goals(mid)
    for goal in goals:
        gk = goal_key(goal)
        if gk not in announced:
            state.announce_goal(mid, gk)
            scorer = (goal.get("scorer") or {}).get("name", "")
            log.info("[GOAL] Match %s — %s' %s", mid, goal.get("minute", "?"), scorer)
            await broadcast(emb.embed_goal(match, detail, goal))

    # Red cards
    announced_c = state.get_announced_cards(mid)
    for card in red_cards:
        ck = card_key(card)
        if ck not in announced_c:
            state.announce_card(mid, ck)
            player = (card.get("player") or {}).get("name", "?")
            log.info("[CARD] Match %s — %s' %s", mid, card.get("minute", "?"), player)
            await broadcast(emb.embed_red_card(match, detail, card))

    # Status transitions
    sent      = state.get_sent(mid)
    prev_stat = snap.get("status", "")

    if status_changed:
        if cur_stat == "PAUSED" and "HT" not in sent:
            state.mark_sent(mid, "HT")
            log.info("[MONITOR] Half-time for match %s", mid)
            await broadcast(emb.embed_halftime(match, detail))

        elif cur_stat == "IN_PLAY" and prev_stat == "PAUSED" and "2H" not in sent:
            state.mark_sent(mid, "2H")
            log.info("[MONITOR] Second half for match %s", mid)
            await broadcast(emb.embed_second_half(match), min_mode="detailed")

        elif cur_stat == "EXTRA_TIME" and "ET" not in sent:
            state.mark_sent(mid, "ET")
            log.info("[MONITOR] Extra time for match %s", mid)
            await broadcast(emb.embed_extra_time(match, detail), min_mode="standard")

        elif cur_stat == "PENALTY_SHOOTOUT" and "PSO" not in sent:
            state.mark_sent(mid, "PSO")
            log.info("[MONITOR] Penalty shootout for match %s", mid)
            await broadcast(emb.embed_penalty_shootout(match, detail), min_mode="standard")

        # MOTM vote — at ~75 min
        minute = match.get("minute") or 0
        if cur_stat == "IN_PLAY" and int(minute) >= 75 and "MOTM" not in sent:
            detail2 = await get_match_detail(match["id"])
            nominees = _build_motm_nominees(detail2 or {})
            if nominees:
                state.mark_sent(mid, "MOTM")
                motm_em   = emb.embed_motm_vote(match)
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


async def _process_fulltime(match: dict, detail: dict | None) -> None:
    mid = str(match["id"])
    log.info("[MONITOR] Processing full time for match %s", mid)

    if detail is None:
        log.warning("[MONITOR] No detail for FT match %s — using basic data", mid)

    await broadcast(emb.embed_fulltime(match, detail or match))

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
                emb.embed_prediction_results(match, h, a, exact_winners, result_winners)
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
                    await ch.send(embed=emb.embed_motm_result(match, winners, tally))
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
                        locked_em = motm_msg.embeds[0]
                        locked_em = locked_em.copy()
                        locked_em.set_footer(text="🔒 Voting closed  •  🏆 FIFA World Cup 2026")
                        await motm_msg.edit(embed=locked_em, view=None)
                except discord.HTTPException:
                    pass

    # Update leaderboards
    for gid, cfg in state.all_guild_configs().items():
        if state.get_leaderboard(gid):
            await _update_leaderboard_in_results(gid, cfg)

    # Schedule recap
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
    await broadcast_summary(emb.embed_full_recap(match, detail, yt))


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
            await ch.send(embed=emb.embed_daily_summary(matches, date_str))
            state.mark_daily_summary_sent(date_key)
            log.info("[SUMMARY] Daily summary sent to guild %s", gid)
        except discord.HTTPException as e:
            log.error("[SUMMARY] Failed for guild %s: %s", gid, e)


@daily_summary_loop.before_loop
async def _before_daily() -> None:
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════════
#  ON_READY
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready() -> None:
    log.info("[BOT] Logged in as %s (ID: %s)", bot.user, bot.user.id)

    # Register persistent views so buttons survive restarts
    bot.add_view(ModePicker())
    bot.add_view(InteractivePanel())
    bot.add_view(DashboardView())

    try:
        synced = await tree.sync()
        log.info("[BOT] Synced %d slash commands", len(synced))
    except Exception as e:
        log.error("[BOT] Slash sync failed: %s", e)

    await load_wc_match_order()

    if not monitor_loop.is_running():
        monitor_loop.start()
    if not daily_summary_loop.is_running():
        daily_summary_loop.start()

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
        await ctx.send(embed=_error_embed("You don't have permission to use this command.", "You need **Manage Channels** permission."))
    elif isinstance(error, commands.CommandNotFound):
        pass  # Silently ignore unknown prefix commands
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
        await interaction.response.send_message(
            embed=_success_embed(f"Dashboard posted in {interaction.channel.mention}."),
            ephemeral=True,
        )
        log.info("[BOT] Dashboard posted for guild %s", gid)
    except discord.HTTPException as e:
        await interaction.response.send_message(
            embed=_error_embed(f"Failed to post dashboard: {e}", "Check that I have Send Messages permission here."),
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
    await _send_embeds(interaction, emb.embed_today(matches))


@bot.command(name="today", help="Today's World Cup matches")
async def prefix_today(ctx):
    async with ctx.typing():
        matches = await get_todays_matches()
    for em in emb.embed_today(matches):
        await ctx.send(embed=em)


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
            embed=_error_embed("Failed to load live matches.", "Try again in a moment — or use `/today` for today's schedule.")
        )
        return
    await interaction.followup.send(embed=emb.embed_live(matches))


@bot.command(name="live", help="Currently live World Cup matches")
async def prefix_live(ctx):
    async with ctx.typing():
        matches = await get_live_matches()
    await ctx.send(embed=emb.embed_live(matches))


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
    await interaction.followup.send(embed=emb.embed_nextmatch(match))


@bot.command(name="nextmatch", help="Next World Cup match")
async def prefix_nextmatch(ctx):
    async with ctx.typing():
        match = await get_next_match()
    if not match:
        await ctx.send(embed=discord.Embed(description="📭 No upcoming matches.", color=emb.C_GREY))
        return
    await ctx.send(embed=emb.embed_nextmatch(match))


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
        await ctx.send(embed=_error_embed("World Cup standings not yet available.", "Standings appear during the group stage."))
        return
    for em in emb.embed_standings(data):
        await ctx.send(embed=em)


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
                f"Valid groups are A through P. Try `/group A` or use autocomplete."
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
    for em in emb.embed_worldcup_overview(data):
        await ctx.send(embed=em)


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
    await _send_embeds(interaction, emb.embed_bracket(knockout))


@bot.command(name="bracket", help="World Cup knockout bracket")
async def prefix_bracket(ctx):
    today = datetime.now(timezone.utc)
    async with ctx.typing():
        matches = await get_competition_matches("2026-06-01", (today + timedelta(days=90)).strftime("%Y-%m-%d"))
    knockout = [m for m in matches if m.get("stage") in ("LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")]
    for em in emb.embed_bracket(knockout):
        await ctx.send(embed=em)


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
    await interaction.followup.send(embed=emb.embed_upcoming(matches, days))


@bot.command(name="upcoming", help="Upcoming matches. Usage: !upcoming 7")
async def prefix_upcoming(ctx, days: int = 7):
    days  = max(1, min(days, 30))
    today = datetime.now(timezone.utc)
    async with ctx.typing():
        matches = await get_competition_matches(
            today.strftime("%Y-%m-%d"),
            (today + timedelta(days=days)).strftime("%Y-%m-%d"),
        )
    await ctx.send(embed=emb.embed_upcoming(matches, days))


# ── /match ────────────────────────────────────────────────────────────────────

_register("Match Commands", "match", "Show details for a specific match by ID")

@tree.command(name="match", description="Show full details for a specific match by ID")
@app_commands.describe(match_id="Match ID from football-data.org")
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
    if status == "FINISHED":
        await interaction.followup.send(embed=emb.embed_fulltime(detail, detail))
    elif status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
        await interaction.followup.send(embed=emb.embed_live([detail]))
    else:
        await interaction.followup.send(embed=emb.embed_nextmatch(detail))


# ── /team ─────────────────────────────────────────────────────────────────────

_register("Match Commands", "team", "Show a national team's profile and World Cup fixtures")

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
        recent, upcoming = await asyncio.gather(
            get_team_matches(tid, status="FINISHED", limit=5),
            get_team_matches(tid, status="SCHEDULED", limit=5),
        )
    except Exception as e:
        log.error("[CMD] /team match fetch error: %s", e)
        recent, upcoming = [], []
    await interaction.followup.send(embed=emb.embed_team(team, recent, upcoming))


@bot.command(name="team", help="Team profile. Usage: !team Brazil")
async def prefix_team(ctx, *, name: str):
    async with ctx.typing():
        team = await search_team(name)
    if not team:
        await ctx.send(embed=_error_embed(f"Team `{name}` not found.", "Try a different spelling."))
        return
    tid = team["id"]
    recent, upcoming = await asyncio.gather(
        get_team_matches(tid, status="FINISHED", limit=5),
        get_team_matches(tid, status="SCHEDULED", limit=5),
    )
    await ctx.send(embed=emb.embed_team(team, recent, upcoming))


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

    standings_snap = []
    if standings_data:
        standings_snap = standings_data.get("standings", [])

    lb = state.get_leaderboard(gid) if gid else {}
    embeds = emb.embed_matchcenter(live, next_m, standings_snap, lb, interaction.guild)
    await _send_embeds(interaction, embeds)


@bot.command(name="matchcenter", help="Live matches + next match + standings + leaderboard")
async def prefix_matchcenter(ctx):
    async with ctx.typing():
        live, next_m, standings_data = await asyncio.gather(
            get_live_matches(), get_next_match(), get_standings()
        )
    standings_snap = standings_data.get("standings", []) if standings_data else []
    lb     = state.get_leaderboard(str(ctx.guild.id)) if ctx.guild else {}
    embeds = emb.embed_matchcenter(live, next_m, standings_snap, lb, ctx.guild)
    for em in embeds:
        await ctx.send(embed=em)


# ── /predict ──────────────────────────────────────────────────────────────────

_register("Predictions", "predict", "Make a score prediction for an upcoming World Cup match")

@tree.command(name="predict", description="Make a score prediction for an upcoming World Cup match")
@app_commands.describe(match_id="Match ID (find with /upcoming or /today)")
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

    existing = state.get_score_predictions(mid).get(str(interaction.user.id))
    view = ScoreInputView(mid, home, away, ko)
    if existing:
        view.home_score = existing["home"]
        view.away_score = existing["away"]
        view.changed    = True
        view._sync()
        content = f"**Your current prediction:** {existing['home']}–{existing['away']}\nUpdate it below."
    else:
        content = view._content()

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

_register("Following", "followteam", "Follow a national team to track their World Cup journey")

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

_register("Following", "unfollowteam", "Unfollow a national team")

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
    teams     = [t for t in following.get("teams", []) if t.lower() != name.strip().lower()]
    following["teams"] = teams
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


# ── /timezone ─────────────────────────────────────────────────────────────────

_register("Settings", "timezone", "Show this server's current timezone")

@tree.command(name="timezone", description="Show the server's current timezone setting")
async def slash_timezone(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            embed=_error_embed("Use this inside a server."), ephemeral=True
        )
        return
    cfg    = state.get_guild_config(str(interaction.guild_id))
    tz_str = cfg.get("timezone", "UTC")
    tz     = _tz_for(interaction.guild_id)
    local  = datetime.now(tz).strftime("%H:%M")
    await interaction.response.send_message(
        embed=discord.Embed(
            description=f"🕐 Server timezone: **{tz_str}**  (current local time: **{local}**)\n"
                        "Daily summary sends at 23:50 in this timezone.",
            color=emb.C_BLUE,
        )
    )


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


@tree.command(name="setchannel", description="Set this channel for live match notifications")
@app_commands.default_permissions(manage_channels=True)
async def slash_setchannel(interaction: discord.Interaction):
    state.set_channel_id(str(interaction.guild_id), "channel_id", str(interaction.channel_id))
    log.info("[ADMIN] Live channel set for guild %s → %s", interaction.guild_id, interaction.channel_id)
    await interaction.response.send_message(embed=_success_embed(
        f"**#{interaction.channel.name}** is now the live match alerts channel.\n"
        "Use `/setmode` to choose notification verbosity."
    ))

_register("Admin", "setchannel", "Set the live match notifications channel")


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
            embed=_error_embed(f"Failed to post panel: {e}", "Make sure I have Send Messages & Embed Links permissions here."),
            ephemeral=True,
        )

_register("Admin", "setinteractivechannel", "Post the user self-service panel")


@tree.command(name="setmode", description="Set guild notification verbosity")
@app_commands.describe(mode="quiet | standard | detailed")
@app_commands.choices(mode=[
    app_commands.Choice(name="🔇 Quiet — Goals + Red Cards + Full Time only",          value="quiet"),
    app_commands.Choice(name="📢 Standard — + HT, ET, MOTM Polls, Predictions",        value="standard"),
    app_commands.Choice(name="📋 Detailed — Everything: lineups, recaps, tables",      value="detailed"),
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
@app_commands.describe(timezone_name="IANA timezone e.g. America/New_York, Europe/London, UTC")
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
            embed=_error_embed("Commands channel not found — it may have been deleted.", "Run `/setcommandschannel` again.")
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
#  PREFIX COMMAND ALIASES (minimal set for legacy support)
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="setchannel")
@commands.has_permissions(manage_channels=True)
async def prefix_setchannel(ctx):
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


@bot.command(name="leaderboard", help="Prediction leaderboard")
async def _prefix_lb(ctx):
    lb = state.get_leaderboard(str(ctx.guild.id))
    await ctx.send(embed=emb.embed_leaderboard(ctx.guild, lb))


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
