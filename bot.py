"""
Kickoff Bot — World Cup & football match notifications for Discord.
Slash (/) commands are the primary interface. Prefix (!) commands need
"Message Content" Privileged Intent enabled in the developer portal.
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
    search_youtube_highlights,
    has_confirmed_lineups,
    parse_dt,
    get_current_score,
    get_score,
    goal_key,
    card_key,
    is_knockout,
    team_display,
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

bot  = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

state.load()

_force_counter: int = 0

# Guild mode ranks (higher = more notifications)
_MODE_RANK = {"quiet": 0, "standard": 1, "detailed": 2}

# ── Per-user notification setting keys + labels ───────────────────────────────

_USER_SETTING_LABELS: dict[str, str] = {
    "goals":               "⚽ Goals",
    "red_cards":           "🟥 Red Cards",
    "kickoff":             "🔔 Kickoff",
    "halftime":            "⏱️ Half Time",
    "fulltime":            "🏁 Full Time",
    "lineups":             "📋 Lineups",
    "motm_vote":           "🌟 MOTM Voting",
    "motm_results":        "🏆 MOTM Results",
    "predictions":         "🎯 Prediction Polls",
    "prediction_results":  "📊 Prediction Results",
    "recaps":              "📺 Recaps",
    "daily_summary":       "📅 Daily Summary",
}

# Default: all settings ON (inherit guild behaviour)
_DEFAULT_USER_SETTINGS: dict[str, bool] = {k: True for k in _USER_SETTING_LABELS}

# ── Inline state helpers for new per-user data ────────────────────────────────
# These extend state._state directly so state.py requires no modifications.

def _get_user_prefs(uid: str, gid: str) -> dict:
    return state._state.setdefault("user_prefs", {}).setdefault(f"{gid}:{uid}", {})

def _set_user_prefs(uid: str, gid: str, prefs: dict) -> None:
    state._state.setdefault("user_prefs", {})[f"{gid}:{uid}"] = prefs
    state.save()

def _get_user_following(uid: str) -> dict:
    return state._state.setdefault("user_following", {}).setdefault(
        uid, {"teams": [], "competitions": []}
    )

def _set_user_following(uid: str, data: dict) -> None:
    state._state.setdefault("user_following", {})[uid] = data
    state.save()

def _get_interactive_panel_msg(gid: str, cid: str) -> str | None:
    return state._state.setdefault("interactive_panels", {}).get(f"{gid}:{cid}")

def _save_interactive_panel_msg(gid: str, cid: str, mid: int) -> None:
    state._state.setdefault("interactive_panels", {})[f"{gid}:{cid}"] = str(mid)
    state.save()


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD UI VIEWS
# ══════════════════════════════════════════════════════════════════════════════

# ── Score prediction views ────────────────────────────────────────────────────

class ScoreInputView(discord.ui.View):
    """Ephemeral per-user score prediction input."""

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

    def _sync(self):
        draw = self.knockout and self.home_score == self.away_score
        self.home_label_btn.label    = f"{self.home_name}  {self.home_score}"
        self.away_label_btn.label    = f"{self.away_name}  {self.away_score}"
        self.home_minus_btn.disabled = (self.home_score == 0)
        self.home_plus_btn.disabled  = (self.home_score >= 9)
        self.away_minus_btn.disabled = (self.away_score == 0)
        self.away_plus_btn.disabled  = (self.away_score >= 9)
        self.lock_btn.disabled       = not self.changed or draw
        self.lock_btn.label          = "⚠️ No draws in knockout" if draw else "🔒 Lock Prediction"

    def _content(self) -> str:
        ko_warn = "\n⚠️ *Draws not allowed in knockout — adjust your scores.*" \
            if self.knockout and self.home_score == self.away_score and self.changed else ""
        return (
            f"**Your prediction:**\n"
            f"🏠 **{self.home_name}**  {self.home_score} – {self.away_score}  **{self.away_name}** ✈️"
            f"{ko_warn}"
        )

    @discord.ui.button(label="−", style=discord.ButtonStyle.secondary, row=0)
    async def home_minus_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.home_score = max(0, self.home_score - 1)
        self.changed = True
        self._sync()
        await interaction.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="Home 0", style=discord.ButtonStyle.primary, row=0, disabled=True)
    async def home_label_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=0)
    async def home_plus_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.home_score = min(9, self.home_score + 1)
        self.changed = True
        self._sync()
        await interaction.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="−", style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def away_minus_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.away_score = max(0, self.away_score - 1)
        self.changed = True
        self._sync()
        await interaction.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="Away 0", style=discord.ButtonStyle.primary, row=1, disabled=True)
    async def away_label_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=1)
    async def away_plus_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.away_score = min(9, self.away_score + 1)
        self.changed = True
        self._sync()
        await interaction.response.edit_message(content=self._content(), view=self)

    @discord.ui.button(label="🔒 Lock Prediction", style=discord.ButtonStyle.success, row=2, disabled=True)
    async def lock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.knockout and self.home_score == self.away_score:
            await interaction.response.send_message(
                "❌ Draws are not allowed in knockout matches. Please change your prediction.",
                ephemeral=True,
            )
            return
        state.save_score_prediction(
            str(interaction.user.id), self.match_id, self.home_score, self.away_score
        )
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                f"🎯 **Locked in: {self.home_score}–{self.away_score}** — "
                f"may the odds be ever in your favour!"
            ),
            view=self,
        )
        self.stop()


class PredictionView(discord.ui.View):
    """Attached to the prediction poll embed. Opens ephemeral ScoreInputView."""

    def __init__(self, match_id: str, home_team: str, away_team: str, knockout: bool = False):
        super().__init__(timeout=5400)
        self.match_id  = match_id
        self.home_team = home_team
        self.away_team = away_team
        self.knockout  = knockout

    @discord.ui.button(label="🎯 Predict Score", style=discord.ButtonStyle.primary)
    async def predict_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ScoreInputView(self.match_id, self.home_team, self.away_team, self.knockout)
        existing = state.get_score_predictions(self.match_id).get(str(interaction.user.id))
        if existing:
            # Pre-fill view with existing prediction
            view.home_score = existing["home"]
            view.away_score = existing["away"]
            view.changed    = True
            view._sync()
            content = (
                f"**Your current prediction:** {existing['home']}–{existing['away']}\n"
                "You can change it below until kick-off."
            )
        else:
            content = view._content()
        await interaction.response.send_message(content=content, view=view, ephemeral=True)


# ── MOTM voting ───────────────────────────────────────────────────────────────

class MotmVoteView(discord.ui.View):
    """Select-menu MOTM voting, sent to predictions_channel."""

    def __init__(self, match_id: str, nominees: list[str]):
        super().__init__(timeout=7200)
        self.match_id = match_id
        options = [discord.SelectOption(label=n[:100], value=n[:100]) for n in nominees[:25]]
        select = discord.ui.Select(
            placeholder="🌟 Vote for Man of the Match...",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self._on_vote
        self.add_item(select)

    async def _on_vote(self, interaction: discord.Interaction):
        selected = interaction.data["values"][0]
        gid   = str(interaction.guild_id)
        votes = state.get_motm_votes(self.match_id, gid)
        votes[str(interaction.user.id)] = selected
        state.save_motm_votes(self.match_id, gid, votes)
        await interaction.response.send_message(
            f"✅ MOTM vote recorded: **{selected}**\nYou can change your vote until full time.",
            ephemeral=True,
        )


# ── Notification mode picker ──────────────────────────────────────────────────

class ModePicker(discord.ui.View):
    """Persistent notification-mode selector posted in commands_channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔇 Quiet", style=discord.ButtonStyle.secondary,
                       custom_id="modepicker_quiet")
    async def quiet(self, interaction: discord.Interaction, button: discord.ui.Button):
        state.set_user_mode(str(interaction.guild_id), str(interaction.user.id), "quiet")
        await interaction.response.send_message(
            "🔇 **Quiet mode** set — you'll receive Goals + Full Time notifications only.",
            ephemeral=True,
        )

    @discord.ui.button(label="📢 Standard", style=discord.ButtonStyle.primary,
                       custom_id="modepicker_standard")
    async def standard(self, interaction: discord.Interaction, button: discord.ui.Button):
        state.set_user_mode(str(interaction.guild_id), str(interaction.user.id), "standard")
        await interaction.response.send_message(
            "📢 **Standard mode** set — Goals, Red Cards, Half-time + Full Time.",
            ephemeral=True,
        )

    @discord.ui.button(label="📋 Detailed", style=discord.ButtonStyle.success,
                       custom_id="modepicker_detailed")
    async def detailed(self, interaction: discord.Interaction, button: discord.ui.Button):
        state.set_user_mode(str(interaction.guild_id), str(interaction.user.id), "detailed")
        await interaction.response.send_message(
            "📋 **Detailed mode** set — everything: lineups, all events, stats, recaps.",
            ephemeral=True,
        )


# ── Per-user notification settings view ──────────────────────────────────────

def _build_settings_embed(uid: str, gid: str) -> discord.Embed:
    prefs = _get_user_prefs(uid, gid)
    em = discord.Embed(
        title="🔔 My Notification Settings",
        description=(
            "Your personal notification preferences for this server.\n"
            "**ON** = receive this notification  |  **OFF** = skip it\n"
            "If all are ON, you follow the server's default mode."
        ),
        color=0x5865F2,
    )
    rows = []
    for key, label in _USER_SETTING_LABELS.items():
        enabled = prefs.get(key, True)
        rows.append(f"{'✅' if enabled else '❌'}  {label}")
    em.add_field(name="Settings", value="\n".join(rows), inline=False)
    em.set_footer(text="Changes apply immediately • Only visible to you")
    return em


class UserSettingsSelect(discord.ui.Select):
    """Multi-select for toggling individual notification types."""

    def __init__(self, uid: str, gid: str):
        self.uid = uid
        self.gid = gid
        prefs    = _get_user_prefs(uid, gid)
        options  = [
            discord.SelectOption(
                label=label,
                value=key,
                description="Currently ON" if prefs.get(key, True) else "Currently OFF",
                emoji="✅" if prefs.get(key, True) else "❌",
                default=False,
            )
            for key, label in _USER_SETTING_LABELS.items()
        ]
        super().__init__(
            placeholder="Select notifications to toggle ON/OFF…",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        prefs = dict(_get_user_prefs(self.uid, self.gid))  # copy
        for key in self.values:
            prefs[key] = not prefs.get(key, True)
        _set_user_prefs(self.uid, self.gid, prefs)

        # Rebuild select with updated descriptions so feedback is immediate
        self.options = [
            discord.SelectOption(
                label=label,
                value=key,
                description="Currently ON" if prefs.get(key, True) else "Currently OFF",
                emoji="✅" if prefs.get(key, True) else "❌",
                default=False,
            )
            for key, label in _USER_SETTING_LABELS.items()
        ]
        await interaction.response.edit_message(
            embed=_build_settings_embed(self.uid, self.gid),
            view=self.view,
        )


class UserSettingsView(discord.ui.View):
    """Ephemeral view for per-user notification settings."""

    def __init__(self, uid: str, gid: str):
        super().__init__(timeout=300)
        self.uid = uid
        self.gid = gid
        self.add_item(UserSettingsSelect(uid, gid))

    @discord.ui.button(label="🔄 Reset to Server Defaults", style=discord.ButtonStyle.danger, row=1)
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        _set_user_prefs(self.uid, self.gid, {})
        # Rebuild select after reset
        self.clear_items()
        self.add_item(UserSettingsSelect(self.uid, self.gid))
        self.add_reset_button()
        await interaction.response.edit_message(
            embed=_build_settings_embed(self.uid, self.gid),
            view=self,
        )

    def add_reset_button(self):
        # Helper used after rebuilding items
        btn = discord.ui.Button(
            label="🔄 Reset to Server Defaults",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        btn.callback = self._reset_callback
        self.add_item(btn)

    async def _reset_callback(self, interaction: discord.Interaction):
        _set_user_prefs(self.uid, self.gid, {})
        self.clear_items()
        self.add_item(UserSettingsSelect(self.uid, self.gid))
        self.add_reset_button()
        await interaction.response.edit_message(
            embed=_build_settings_embed(self.uid, self.gid),
            view=self,
        )


# ── Follow-team ephemeral view ────────────────────────────────────────────────

def _build_teams_embed(uid: str) -> discord.Embed:
    following = _get_user_following(uid)
    teams = following.get("teams", [])
    em = discord.Embed(
        title="⭐ My Followed Teams",
        color=0x57F287,
    )
    if teams:
        em.description = "\n".join(f"• {t}" for t in teams)
    else:
        em.description = "You're not following any teams yet.\nUse `/followteam <name>` to start."
    em.set_footer(text="You'll get notified when followed teams play")
    return em


def _build_competitions_embed(uid: str) -> discord.Embed:
    following = _get_user_following(uid)
    comps = following.get("competitions", [])
    em = discord.Embed(
        title="🏆 My Followed Competitions",
        color=0xFEE75C,
    )
    if comps:
        em.description = "\n".join(f"• {c}" for c in comps)
    else:
        em.description = "You're not following any competitions yet.\nUse `/followcompetition <code>` to start."
    em.set_footer(text="e.g. WC, PL, CL, EURO")
    return em


class UnfollowTeamView(discord.ui.View):
    """Ephemeral view showing followed teams with an unfollow select."""

    def __init__(self, uid: str):
        super().__init__(timeout=120)
        self.uid = uid
        self._rebuild_items()

    def _rebuild_items(self):
        self.clear_items()
        following = _get_user_following(self.uid)
        teams = following.get("teams", [])
        if teams:
            options = [discord.SelectOption(label=t[:100], value=t[:100]) for t in teams[:25]]
            select = discord.ui.Select(
                placeholder="Select a team to unfollow…",
                min_values=1,
                max_values=1,
                options=options,
            )
            select.callback = self._on_unfollow
            self.add_item(select)

    async def _on_unfollow(self, interaction: discord.Interaction):
        team_name = interaction.data["values"][0]
        following = dict(_get_user_following(self.uid))
        teams = [t for t in following.get("teams", []) if t != team_name]
        following["teams"] = teams
        _set_user_following(self.uid, following)
        self._rebuild_items()
        await interaction.response.edit_message(
            embed=_build_teams_embed(self.uid),
            view=self if following.get("teams") else None,
        )


class UnfollowCompetitionView(discord.ui.View):
    """Ephemeral view showing followed competitions with unfollow select."""

    def __init__(self, uid: str):
        super().__init__(timeout=120)
        self.uid = uid
        self._rebuild_items()

    def _rebuild_items(self):
        self.clear_items()
        following = _get_user_following(self.uid)
        comps = following.get("competitions", [])
        if comps:
            options = [discord.SelectOption(label=c[:100], value=c[:100]) for c in comps[:25]]
            select = discord.ui.Select(
                placeholder="Select a competition to unfollow…",
                min_values=1,
                max_values=1,
                options=options,
            )
            select.callback = self._on_unfollow
            self.add_item(select)

    async def _on_unfollow(self, interaction: discord.Interaction):
        comp_code = interaction.data["values"][0]
        following = dict(_get_user_following(self.uid))
        comps = [c for c in following.get("competitions", []) if c != comp_code]
        following["competitions"] = comps
        _set_user_following(self.uid, following)
        self._rebuild_items()
        await interaction.response.edit_message(
            embed=_build_competitions_embed(self.uid),
            view=self if following.get("competitions") else None,
        )


# ── Interactive panel (persistent, posted by admin to any channel) ────────────

class InteractivePanel(discord.ui.View):
    """
    Persistent panel admins can post to any channel.
    Every button opens an ephemeral interaction visible only to the clicking user.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔔 My Notifications",
        style=discord.ButtonStyle.primary,
        custom_id="ipanel_notifications",
        row=0,
    )
    async def btn_notifications(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        gid = str(interaction.guild_id)
        view = UserSettingsView(uid, gid)
        await interaction.response.send_message(
            embed=_build_settings_embed(uid, gid),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="⭐ My Teams",
        style=discord.ButtonStyle.secondary,
        custom_id="ipanel_teams",
        row=0,
    )
    async def btn_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        following = _get_user_following(uid)
        teams = following.get("teams", [])
        em = _build_teams_embed(uid)
        view = UnfollowTeamView(uid) if teams else None
        await interaction.response.send_message(embed=em, view=view, ephemeral=True)

    @discord.ui.button(
        label="🏆 My Competitions",
        style=discord.ButtonStyle.secondary,
        custom_id="ipanel_competitions",
        row=0,
    )
    async def btn_competitions(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        following = _get_user_following(uid)
        comps = following.get("competitions", [])
        em = _build_competitions_embed(uid)
        view = UnfollowCompetitionView(uid) if comps else None
        await interaction.response.send_message(embed=em, view=view, ephemeral=True)

    @discord.ui.button(
        label="🏅 Leaderboard",
        style=discord.ButtonStyle.success,
        custom_id="ipanel_leaderboard",
        row=1,
    )
    async def btn_leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return
        lb = state.get_leaderboard(str(interaction.guild_id))
        await interaction.response.send_message(
            embed=emb.embed_leaderboard(interaction.guild, lb),
            ephemeral=True,
        )

    @discord.ui.button(
        label="📅 Today's Matches",
        style=discord.ButtonStyle.success,
        custom_id="ipanel_today",
        row=1,
    )
    async def btn_today(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        matches = await get_todays_matches()
        embeds  = emb.embed_today(matches)
        # send first embed, followups for the rest
        await interaction.followup.send(embed=embeds[0], ephemeral=True)
        for em_item in embeds[1:]:
            await interaction.followup.send(embed=em_item, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _guild_mode(guild_id) -> str:
    return state.get_guild_config(str(guild_id)).get("mode", "standard")


def _mode_at_least(guild_id, min_mode: str) -> bool:
    return _MODE_RANK.get(_guild_mode(guild_id), 1) >= _MODE_RANK.get(min_mode, 0)


async def broadcast(embed: discord.Embed, reactions: list[str] | None = None,
                    min_mode: str = "quiet") -> None:
    """Send to every guild's matches channel_id that meets min_mode."""
    for gid, cfg in state.all_guild_configs().items():
        cid = cfg.get("channel_id")
        if not cid:
            continue
        if not _mode_at_least(gid, min_mode):
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


async def broadcast_predictions(embed: discord.Embed,
                                 view: discord.ui.View | None = None,
                                 match_id: str | None = None) -> None:
    """Send to every guild's predictions_channel_id (standard+)."""
    for gid, cfg in state.all_guild_configs().items():
        if not _mode_at_least(gid, "standard"):
            continue
        cid = cfg.get("predictions_channel_id")
        if not cid:
            continue
        channel = bot.get_channel(int(cid))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            continue
        try:
            msg = await channel.send(embed=embed, view=view)
            if match_id:
                state.save_poll_message_id(match_id, gid, msg.id)
        except discord.HTTPException as e:
            log.error("[ERROR] predictions broadcast to %s: %s", cid, e)


async def broadcast_results(embed: discord.Embed) -> None:
    """Send to every guild's results_channel_id."""
    for gid, cfg in state.all_guild_configs().items():
        cid = cfg.get("results_channel_id")
        if not cid:
            continue
        channel = bot.get_channel(int(cid))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            continue
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.error("[ERROR] results broadcast to %s: %s", cid, e)


async def broadcast_summary(embed: discord.Embed) -> None:
    """Send to every guild's summary_channel_id (match reports, recaps, highlights)."""
    for gid, cfg in state.all_guild_configs().items():
        cid = cfg.get("summary_channel_id")
        if not cid:
            continue
        channel = bot.get_channel(int(cid))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            continue
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.error("[ERROR] summary broadcast to %s: %s", cid, e)


async def _update_leaderboard_in_results(guild_id: str, cfg: dict) -> None:
    """Send updated leaderboard to results_channel_id; pin newest, unpin previous."""
    cid = cfg.get("results_channel_id")
    if not cid:
        return
    channel = bot.get_channel(int(cid))
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return
    lb = state.get_leaderboard(guild_id)
    try:
        msg = await channel.send(embed=emb.embed_leaderboard(guild, lb))
        old_id = state.get_pinned_leaderboard_message(guild_id)
        if old_id:
            try:
                old = await channel.fetch_message(int(old_id))
                await old.unpin()
            except discord.HTTPException:
                pass
        await msg.pin()
        state.save_pinned_leaderboard_message(guild_id, msg.id)
    except discord.HTTPException as e:
        log.error("[ERROR] Leaderboard post failed for guild %s: %s", guild_id, e)


def any_channel_configured() -> bool:
    return any(v.get("channel_id") for v in state.all_guild_configs().values())


def _tz_for(guild_id) -> ZoneInfo:
    cfg = state.get_guild_config(str(guild_id))
    try:
        return ZoneInfo(cfg.get("timezone", "UTC"))
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


async def _safe_defer(interaction: discord.Interaction) -> None:
    try:
        await interaction.response.defer(thinking=True)
    except discord.HTTPException:
        pass


async def _send_embeds(target, embed_list: list[discord.Embed]) -> None:
    for em in embed_list:
        if isinstance(target, discord.Interaction):
            await target.followup.send(embed=em)
        else:
            await target.send(embed=em)


def _same_result(ph: int, pa: int, h: int, a: int) -> bool:
    if ph > pa and h > a:   return True   # home win
    if ph < pa and h < a:   return True   # away win
    if ph == pa and h == a: return True   # draw
    return False


# ── MOTM nominee generation ───────────────────────────────────────────────────
# FIX: expanded position matching + baseline all outfield players so we always
#      reach 8–10 nominees rather than stopping at 4.

_POS_WEIGHT: dict[str, float] = {
    "ATTACKER":   0.50,  "FORWARD":    0.50,  "FWD": 0.50,
    "MIDFIELDER": 0.35,  "MID":        0.35,
    "DEFENDER":   0.15,  "DEF":        0.15,
    "GOALKEEPER": 0.00,  "GK":         0.00,
    "WING":       0.40,  "WINGER":     0.40,
    "STRIKER":    0.50,  "ST":         0.50,
    "CAM":        0.40,  "CM":         0.35,
    "CDM":        0.25,  "LB":         0.15,
    "RB":         0.15,  "CB":         0.10,
}


def _get_motm_nominees(detail: dict) -> list[str]:
    """
    Build MOTM nominee list targeting 8–10 names.
    Scoring: goal scorer +2, assist +1, position weight, captain +0.3.
    All outfield starters included as baseline so we always reach the target.
    """
    performers: dict[str, float] = {}

    # 1. Goal scorers and assisters
    goals = detail.get("goals") or []
    for g in goals:
        if g.get("type") != "OWN":
            name = (g.get("scorer") or {}).get("name")
            if name:
                performers[name] = performers.get(name, 0.0) + 2.0
        assist_name = (g.get("assist") or {}).get("name")
        if assist_name:
            performers[assist_name] = performers.get(assist_name, 0.0) + 1.0

    # 2. Baseline all starting lineup players with position weights
    home_lu = (detail.get("homeTeam") or {}).get("lineup") or []
    away_lu = (detail.get("awayTeam") or {}).get("lineup") or []
    all_players = home_lu + away_lu

    for p in all_players:
        name = p.get("name")
        if not name:
            continue
        pos    = (p.get("position") or "").upper().strip()
        weight = _POS_WEIGHT.get(pos, 0.20)   # default for unknown positions
        if name not in performers:
            performers[name] = weight
        else:
            performers[name] += weight   # adds to goal/assist pts already accumulated

    # 3. Captain bonus
    for side in ("homeTeam", "awayTeam"):
        cap_name = ((detail.get(side) or {}).get("captain") or {}).get("name")
        if cap_name and cap_name in performers:
            performers[cap_name] += 0.30

    # 4. Sort and build list
    sorted_nominees = sorted(performers.items(), key=lambda x: x[1], reverse=True)
    nominees = [n for n, _ in sorted_nominees]

    # 5. If still short of 8, pad from bench/substitute list (excluding GKs first)
    if len(nominees) < 8:
        home_sq = (detail.get("homeTeam") or {}).get("bench") or []
        away_sq = (detail.get("awayTeam") or {}).get("bench") or []
        for p in home_sq + away_sq:
            name = p.get("name")
            pos  = (p.get("position") or "").upper()
            if name and name not in nominees and pos != "GOALKEEPER" and pos != "GK":
                nominees.append(name)
                if len(nominees) >= 10:
                    break

    # 6. Last resort — fill from raw lineup order if still short
    if len(nominees) < 8:
        for p in all_players:
            name = p.get("name")
            if name and name not in nominees:
                nominees.append(name)
                if len(nominees) >= 10:
                    break

    return nominees[:10]


async def _close_prediction_poll(match_id: str) -> None:
    """Edit prediction poll message to 'Predictions closed' and remove view."""
    for gid, cfg in state.all_guild_configs().items():
        cid    = cfg.get("predictions_channel_id")
        msg_id = state.get_poll_message_id(match_id, gid)
        if not cid or not msg_id:
            continue
        ch = bot.get_channel(int(cid))
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(int(msg_id))
            if msg.embeds:
                em_dict = msg.embeds[0].to_dict()
                em_dict["footer"] = {"text": "🔒 Predictions closed  •  football-data.org"}
                await msg.edit(embed=discord.Embed.from_dict(em_dict), view=None)
        except discord.HTTPException:
            pass


async def _send_motm_vote(match: dict, detail: dict) -> None:
    """Send MOTM vote to predictions_channel for all standard+ guilds."""
    mid      = str(match["id"])
    nominees = _get_motm_nominees(detail)
    if not nominees:
        log.warning("[MATCH] No MOTM nominees found for match %s", mid)
        return

    log.info("[MATCH] MOTM nominees for match %s: %s", mid, nominees)

    for gid, cfg in state.all_guild_configs().items():
        if not _mode_at_least(gid, "standard"):
            continue
        cid = cfg.get("predictions_channel_id")
        if not cid:
            continue
        ch = bot.get_channel(int(cid))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            continue
        try:
            view = MotmVoteView(mid, nominees)
            msg  = await ch.send(embed=emb.embed_motm_vote(match, nominees), view=view)
            state.save_motm_message_id(mid, gid, msg.id)
            log.info("[MATCH] MOTM vote sent for match %s to guild %s", mid, gid)
        except discord.HTTPException as e:
            log.error("[ERROR] MOTM vote send failed for guild %s: %s", gid, e)


# ══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND LOOPS
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(minutes=2)
async def monitor_loop() -> None:
    global _force_counter
    _force_counter += 1
    force_refresh = (_force_counter % 6 == 0)

    if not any_channel_configured():
        return

    try:
        matches = await get_todays_matches()
    except Exception as e:
        log.error("[ERROR] monitor_loop fetch failed: %s", e)
        return

    now = datetime.now(timezone.utc)

    for match in matches:
        mid    = str(match["id"])
        status = match.get("status", "")
        ko_dt  = parse_dt(match.get("utcDate"))
        sent   = state.get_sent(mid)

        # ── Pre-match ─────────────────────────────────────────────────────
        if status in ("SCHEDULED", "TIMED") and ko_dt:
            mins = (ko_dt - now).total_seconds() / 60

            # Score prediction poll: 85–95 min before kickoff
            if 85 <= mins <= 95 and "poll" not in sent:
                state.mark_sent(mid, "poll")
                log.info("[MATCH] Score prediction poll for match %s", mid)
                home = team_display(match.get("homeTeam", {}))
                away = team_display(match.get("awayTeam", {}))
                ko   = is_knockout(match)
                view = PredictionView(mid, home, away, ko)
                await broadcast_predictions(
                    emb.embed_prediction_prompt(match),
                    view=view,
                    match_id=mid,
                )

            # Lineups: 55–70 min before kickoff
            if 55 <= mins <= 70 and "lineups" not in sent:
                detail = await get_match_detail(match["id"])
                if detail and has_confirmed_lineups(detail):
                    state.mark_sent(mid, "lineups")
                    log.info("[MATCH] Lineups for match %s", mid)
                    await broadcast(emb.embed_lineup(match, detail), min_mode="detailed")

            # 15-min reminder — NOTE: if embeds.py embed_reminder() shows wrong text,
            # verify that function uses the `minutes` parameter, not a hardcoded string.
            # The value passed here is always 15 (minutes), which is correct.
            if 10 <= mins <= 20 and "15m" not in sent:
                state.mark_sent(mid, "15m")
                log.info("[MATCH] 15-min reminder for match %s", mid)
                await broadcast(emb.embed_reminder(match, 15))

        # ── Kick-off ───────────────────────────────────────────────────────
        if status == "IN_PLAY" and "kickoff" not in sent:
            state.mark_sent(mid, "kickoff")
            log.info("[MATCH] Kick-off for match %s", mid)
            await broadcast(emb.embed_kickoff(match))
            asyncio.create_task(_close_prediction_poll(mid))

        # ── Live event detection ───────────────────────────────────────────
        if status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
            snap        = state.get_snapshot(mid)
            h, a        = get_current_score(match)
            prev_h      = snap.get("home_score")
            prev_a      = snap.get("away_score")
            prev_status = snap.get("status", "")

            score_changed  = h is not None and (h != prev_h or a != prev_a)
            status_changed = status != prev_status

            if score_changed or status_changed or force_refresh:
                await _process_live_detail(match, snap, score_changed, status_changed)
            elif not snap:
                state.set_snapshot(mid, {"home_score": h, "away_score": a, "status": status})

            # MOTM vote at ~75' game time (elapsed ≈ 90 min including HT break)
            if "MOTM" not in sent and ko_dt:
                elapsed_min = (now - ko_dt).total_seconds() / 60
                if elapsed_min >= 90 and status in ("IN_PLAY", "PAUSED"):
                    state.mark_sent(mid, "MOTM")
                    log.info("[MATCH] Triggering MOTM vote for match %s", mid)
                    detail = await get_match_detail(match["id"])
                    if detail:
                        asyncio.create_task(_send_motm_vote(match, detail))

        # ── Full-time ─────────────────────────────────────────────────────
        if status == "FINISHED" and "FT" not in sent:
            state.mark_sent(mid, "FT")
            detail = await get_match_detail(match["id"])
            log.info("[MATCH] FT for match %s", mid)
            await _process_fulltime(match, detail)


async def _process_live_detail(
    match: dict,
    snap: dict,
    score_changed: bool,
    status_changed: bool,
) -> None:
    """Fetch detail for a live match, fire embeds for any new events."""
    mid    = str(match["id"])
    detail = await get_match_detail(match["id"])
    if not detail:
        return

    goals     = detail.get("goals") or []
    bookings  = detail.get("bookings") or []
    red_cards = [b for b in bookings if b.get("card") in ("RED", "YELLOW_RED")]
    cur_status = match.get("status", "")
    h, a       = get_current_score(match)

    # Goals (all modes)
    announced_goals = state.get_announced_goals(mid)
    for goal in goals:
        gk = goal_key(goal)
        if gk not in announced_goals:
            state.announce_goal(mid, gk)
            scorer = (goal.get("scorer") or {}).get("name", "")
            log.info("[GOAL] Match %s — %s' %s", mid, goal.get("minute", "?"), scorer)
            await broadcast(emb.embed_goal(match, detail, goal))

    # Red cards (all modes)
    announced_cards = state.get_announced_cards(mid)
    for card in red_cards:
        ck = card_key(card)
        if ck not in announced_cards:
            state.announce_card(mid, ck)
            player = (card.get("player") or {}).get("name", "unknown")
            log.info("[CARD] Match %s — %s' %s", mid, card.get("minute", "?"), player)
            await broadcast(emb.embed_red_card(match, detail, card))

    # Status transitions
    sent        = state.get_sent(mid)
    prev_status = snap.get("status", "")

    if status_changed:
        if cur_status == "PAUSED" and "HT" not in sent:
            state.mark_sent(mid, "HT")
            log.info("[MATCH] Half-time for match %s", mid)
            await broadcast(emb.embed_halftime(match, detail))

        elif cur_status == "IN_PLAY" and prev_status == "PAUSED" and "2H" not in sent:
            state.mark_sent(mid, "2H")
            log.info("[MATCH] Second half for match %s", mid)
            await broadcast(emb.embed_second_half(match), min_mode="detailed")

        elif cur_status == "EXTRA_TIME" and "ET" not in sent:
            state.mark_sent(mid, "ET")
            log.info("[MATCH] Extra time for match %s", mid)
            await broadcast(emb.embed_extra_time(match, detail), min_mode="standard")

        elif cur_status == "PENALTY_SHOOTOUT" and "PSO" not in sent:
            state.mark_sent(mid, "PSO")
            log.info("[MATCH] Penalty shootout for match %s", mid)
            await broadcast(emb.embed_penalty_shootout(match, detail), min_mode="standard")

    state.set_snapshot(mid, {"home_score": h, "away_score": a, "status": cur_status})


async def _process_fulltime(match: dict, detail: dict) -> None:
    """Comprehensive FT handler: embed + score predictions + MOTM + leaderboard + recap."""
    mid = str(match["id"])

    # 1. FT embed → channel_id (all modes)
    await broadcast(emb.embed_fulltime(match, detail))

    # 2. Score predictions scoring
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
            log.info("[MATCH] FT prediction scoring: %d exact, %d result winners",
                     len(exact_winners), len(result_winners))
            await broadcast_results(
                emb.embed_prediction_results(match, h, a, exact_winners, result_winners)
            )

    # 3. MOTM results (per-guild)
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
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    await ch.send(embed=emb.embed_motm_result(match, winners, tally))
                except discord.HTTPException:
                    pass

        pred_cid = cfg.get("predictions_channel_id")
        if pred_cid and motm_msg_id:
            ch = bot.get_channel(int(pred_cid))
            if ch:
                try:
                    motm_msg = await ch.fetch_message(int(motm_msg_id))
                    if motm_msg.embeds:
                        em_dict = motm_msg.embeds[0].to_dict()
                        em_dict["footer"] = {"text": "🔒 Voting closed  •  football-data.org"}
                        await motm_msg.edit(embed=discord.Embed.from_dict(em_dict), view=None)
                except discord.HTTPException:
                    pass

        log.info("[MATCH] MOTM results processed for guild %s, match %s", gid, mid)

    # 4. Update leaderboard per guild
    for gid, cfg in state.all_guild_configs().items():
        if state.get_leaderboard(gid):
            await _update_leaderboard_in_results(gid, cfg)

    # 5. Schedule 1-hour recap
    asyncio.create_task(_post_delayed_recap(match, 3600))


async def _post_delayed_recap(match: dict, delay: int) -> None:
    """After `delay` seconds, fetch stats, search YouTube, post full recap."""
    await asyncio.sleep(delay)
    mid  = str(match["id"])
    log.info("[MATCH] Posting 1-hour recap for match %s", mid)

    detail = await get_match_detail(match["id"])
    home   = match.get("homeTeam", {}).get("shortName") or match.get("homeTeam", {}).get("name", "Team A")
    away   = match.get("awayTeam", {}).get("shortName") or match.get("awayTeam", {}).get("name", "Team B")
    query  = f"{home} vs {away} {datetime.now(timezone.utc).strftime('%Y')}"

    youtube = await search_youtube_highlights(query)

    await broadcast_summary(emb.embed_full_recap(match, detail, youtube))
    log.info("[MATCH] Recap posted for match %s (YouTube: %s)", mid, "found" if youtube else "not found")


@monitor_loop.before_loop
async def _before_monitor():
    await bot.wait_until_ready()
    await asyncio.sleep(3)


# ── Daily summary + group tables ───────────────────────────────────────────────

@tasks.loop(minutes=5)
async def daily_summary_loop() -> None:
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

        tz        = _tz_for(gid)
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

            if _mode_at_least(gid, "detailed"):
                await _post_group_tables(gid, cfg)
        except discord.HTTPException as e:
            log.error("[SUMMARY] Failed to send to guild %s: %s", gid, e)


async def _post_group_tables(guild_id: str, cfg: dict) -> None:
    """Post WC group stage standings and pin the newest message."""
    cid = cfg.get("channel_id")
    if not cid:
        return
    ch = bot.get_channel(int(cid))
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return

    data = await get_standings("WC")
    if not data:
        return

    tables = data.get("standings", [])
    group_tables = [t for t in tables if "GROUP" in (t.get("group") or "")]
    if not group_tables:
        return

    last_msg = None
    for block in group_tables:
        group_raw = block.get("group", "")
        letter    = group_raw.replace("GROUP_", "") if group_raw else "?"
        try:
            last_msg = await ch.send(embed=emb.embed_wc_group_update(letter, block.get("table", [])))
        except discord.HTTPException:
            pass

    if last_msg:
        old_id = state.get_pinned_table_message(guild_id)
        if old_id:
            try:
                old = await ch.fetch_message(int(old_id))
                await old.unpin()
            except discord.HTTPException:
                pass
        try:
            await last_msg.pin()
            state.save_pinned_table_message(guild_id, last_msg.id)
        except discord.HTTPException:
            pass


@daily_summary_loop.before_loop
async def _before_daily():
    await bot.wait_until_ready()


# ── Commands menu startup ──────────────────────────────────────────────────────

async def _post_commands_menu_if_needed() -> None:
    """Post the commands menu + ModePicker to each guild's commands_channel if not already there."""
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
            log.info("[MATCH] Commands menu posted for guild %s", gid)
        except discord.HTTPException as e:
            log.error("[ERROR] Commands menu post failed for guild %s: %s", gid, e)


# ══════════════════════════════════════════════════════════════════════════════
#  ON_READY
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready() -> None:
    log.info("[MATCH] Logged in as %s (ID: %s)", bot.user, bot.user.id)

    # Re-register persistent views so button interactions survive restarts
    bot.add_view(ModePicker())
    bot.add_view(InteractivePanel())

    try:
        synced = await tree.sync()
        log.info("[MATCH] Synced %d slash commands", len(synced))
    except Exception as e:
        log.error("[ERROR] Slash sync failed: %s", e)

    await load_wc_match_order()

    monitor_loop.start()
    daily_summary_loop.start()

    await _post_commands_menu_if_needed()
    await broadcast(emb.embed_startup(bot.user))
    log.info("[MATCH] Startup complete — %d guilds configured", len(state.all_guild_configs()))


# ══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — slash + prefix pairs
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


# ── /matchtoday ───────────────────────────────────────────────────────────────

@tree.command(name="matchtoday", description="Today's matches, optionally filtered by competition")
@app_commands.describe(competition="Competition code e.g. WC, CL — leave blank for all")
async def slash_matchtoday(interaction: discord.Interaction, competition: str = ""):
    await _safe_defer(interaction)
    matches = await get_todays_matches()
    if competition:
        code    = competition.upper()
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
            embed=discord.Embed(description="📭 No upcoming matches in the next 3 days.", color=emb.C_GREY)
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
        await interaction.followup.send(
            embed=discord.Embed(description=f"❌ No data for `{competition.upper()}`.", color=emb.C_RED)
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
        code, today.strftime("%Y-%m-%d"),
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
            code, today.strftime("%Y-%m-%d"),
            (today + timedelta(days=days)).strftime("%Y-%m-%d"),
        )
    await ctx.send(embed=emb.embed_upcoming(matches, code, days))


# ── /team  !team ──────────────────────────────────────────────────────────────

@tree.command(name="team", description="Show team profile and fixtures")
@app_commands.describe(name="Team name e.g. Brazil, Germany")
async def slash_team(interaction: discord.Interaction, name: str):
    await _safe_defer(interaction)
    team = await search_team(name)
    if not team:
        await interaction.followup.send(
            embed=discord.Embed(description=f"❌ Team `{name}` not found.", color=emb.C_RED)
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


# ── /group  !group ────────────────────────────────────────────────────────────

def _find_group_block(tables: list[dict], letter: str) -> dict | None:
    target = letter.strip().upper()
    exact  = next((t for t in tables if t.get("group") == f"GROUP_{target}"), None)
    if exact:
        return exact
    suffix = next((t for t in tables if (t.get("group") or "").upper().endswith(target)), None)
    if suffix:
        return suffix
    return next((t for t in tables if (t.get("group") or "").upper() == target), None)


@tree.command(name="group", description="World Cup group table — e.g. /group A  (groups A–P)")
@app_commands.describe(letter="Group letter A–P")
async def slash_group(interaction: discord.Interaction, letter: str):
    await _safe_defer(interaction)
    data = await get_standings("WC")
    if not data:
        await interaction.followup.send(
            embed=discord.Embed(
                description="❌ World Cup standings not available yet.\nThey appear once the group stage begins.",
                color=emb.C_RED,
            )
        )
        return
    tables = data.get("standings", [])
    block  = _find_group_block(tables, letter)
    if not block:
        available = ", ".join(
            f"`{t.get('group','').replace('GROUP_','')}`" for t in tables if t.get("group")
        ) or "none yet"
        await interaction.followup.send(
            embed=discord.Embed(
                description=f"❌ Group **{letter.upper()}** not found.\nAvailable groups: {available}",
                color=emb.C_RED,
            )
        )
        return
    actual = (block.get("group") or letter).replace("GROUP_", "")
    await interaction.followup.send(embed=emb.embed_wc_group(actual, block.get("table", [])))


@bot.command(name="group", help="WC group table. Usage: !group A")
async def prefix_group(ctx, letter: str = "A"):
    async with ctx.typing():
        data = await get_standings("WC")
    if not data:
        await ctx.send(embed=discord.Embed(description="❌ WC standings not available yet.", color=emb.C_RED))
        return
    tables = data.get("standings", [])
    block  = _find_group_block(tables, letter)
    if not block:
        available = ", ".join(
            f"`{t.get('group','').replace('GROUP_','')}`" for t in tables if t.get("group")
        ) or "none yet"
        await ctx.send(embed=discord.Embed(
            description=f"❌ Group **{letter.upper()}** not found.\nAvailable: {available}",
            color=emb.C_RED,
        ))
        return
    actual = (block.get("group") or letter).replace("GROUP_", "")
    await ctx.send(embed=emb.embed_wc_group(actual, block.get("table", [])))


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
    today   = datetime.now(timezone.utc)
    matches = await get_competition_matches(
        "WC", "2026-01-01", (today + timedelta(days=60)).strftime("%Y-%m-%d")
    )
    knockout = [m for m in matches if m.get("stage") in
                ("LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")]
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


# ── /match  !match ────────────────────────────────────────────────────────────

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


# ── /leaderboard  !leaderboard ───────────────────────────────────────────────

@tree.command(name="leaderboard", description="Show the prediction leaderboard")
async def slash_leaderboard(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    lb = state.get_leaderboard(str(interaction.guild_id))
    await interaction.response.send_message(embed=emb.embed_leaderboard(interaction.guild, lb))


@bot.command(name="leaderboard", help="Show prediction leaderboard")
async def prefix_leaderboard(ctx):
    lb = state.get_leaderboard(str(ctx.guild.id))
    await ctx.send(embed=emb.embed_leaderboard(ctx.guild, lb))


# ── /resetleaderboard ─────────────────────────────────────────────────────────

@tree.command(name="resetleaderboard", description="Reset the prediction leaderboard (admin)")
@app_commands.default_permissions(administrator=True)
async def slash_resetleaderboard(interaction: discord.Interaction):
    state.reset_leaderboard(str(interaction.guild_id))
    await interaction.response.send_message(
        embed=discord.Embed(description="🗑️ Leaderboard has been reset.", color=emb.C_ORANGE)
    )


@bot.command(name="resetleaderboard", help="Reset leaderboard (admin)")
@commands.has_permissions(administrator=True)
async def prefix_resetleaderboard(ctx):
    state.reset_leaderboard(str(ctx.guild.id))
    await ctx.send(embed=discord.Embed(description="🗑️ Leaderboard has been reset.", color=emb.C_ORANGE))


# ── Channel setup commands ────────────────────────────────────────────────────

def _channel_set_embed(channel: discord.TextChannel, purpose: str) -> discord.Embed:
    return discord.Embed(
        description=f"✅ **#{channel.name}** set as the **{purpose}** channel.",
        color=emb.C_GREEN,
    )


@tree.command(name="setchannel", description="Set this channel for live match notifications")
@app_commands.default_permissions(manage_channels=True)
async def slash_setchannel(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    state.set_channel_id(gid, "channel_id", str(interaction.channel_id))
    log.info("[MATCH] Notification channel set for guild %s → %s", gid, interaction.channel_id)
    await interaction.response.send_message(embed=discord.Embed(
        description=(
            f"✅ **#{interaction.channel.name}** is now the **live match** channel.\n"
            "Monitoring starts automatically — use `/setmode` to choose quiet / standard / detailed."
        ),
        color=emb.C_GREEN,
    ))


@bot.command(name="setchannel", help="Set this channel for match notifications")
@commands.has_permissions(manage_channels=True)
async def prefix_setchannel(ctx):
    state.set_channel_id(str(ctx.guild.id), "channel_id", str(ctx.channel.id))
    await ctx.send(embed=_channel_set_embed(ctx.channel, "live match notifications"))


@tree.command(name="setpredictionschannel", description="Set this channel for prediction polls and MOTM voting")
@app_commands.default_permissions(manage_channels=True)
async def slash_setpredictionschannel(interaction: discord.Interaction):
    state.set_channel_id(str(interaction.guild_id), "predictions_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(embed=_channel_set_embed(interaction.channel, "prediction polls & MOTM voting"))


@bot.command(name="setpredictionschannel", help="Set predictions channel")
@commands.has_permissions(manage_channels=True)
async def prefix_setpredictionschannel(ctx):
    state.set_channel_id(str(ctx.guild.id), "predictions_channel_id", str(ctx.channel.id))
    await ctx.send(embed=_channel_set_embed(ctx.channel, "prediction polls & MOTM voting"))


@tree.command(name="setresultschannel", description="Set this channel for prediction results and MOTM winners")
@app_commands.default_permissions(manage_channels=True)
async def slash_setresultschannel(interaction: discord.Interaction):
    state.set_channel_id(str(interaction.guild_id), "results_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(embed=_channel_set_embed(interaction.channel, "prediction results"))


@bot.command(name="setresultschannel", help="Set results channel")
@commands.has_permissions(manage_channels=True)
async def prefix_setresultschannel(ctx):
    state.set_channel_id(str(ctx.guild.id), "results_channel_id", str(ctx.channel.id))
    await ctx.send(embed=_channel_set_embed(ctx.channel, "prediction results"))


@tree.command(name="setsummarychannel", description="Set this channel for match reports, recaps, and highlights")
@app_commands.default_permissions(manage_channels=True)
async def slash_setsummarychannel(interaction: discord.Interaction):
    state.set_channel_id(str(interaction.guild_id), "summary_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(embed=_channel_set_embed(interaction.channel, "match reports & highlights"))


@bot.command(name="setsummarychannel", help="Set summary channel (recaps & highlights)")
@commands.has_permissions(manage_channels=True)
async def prefix_setsummarychannel(ctx):
    state.set_channel_id(str(ctx.guild.id), "summary_channel_id", str(ctx.channel.id))
    await ctx.send(embed=_channel_set_embed(ctx.channel, "match reports & highlights"))


@tree.command(name="setcommandschannel", description="Set this channel for the command menu and mode picker")
@app_commands.default_permissions(manage_channels=True)
async def slash_setcommandschannel(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    state.set_channel_id(gid, "commands_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(embed=_channel_set_embed(interaction.channel, "command menu"))
    await _post_commands_menu_if_needed()


@bot.command(name="setcommandschannel", help="Set commands menu channel")
@commands.has_permissions(manage_channels=True)
async def prefix_setcommandschannel(ctx):
    gid = str(ctx.guild.id)
    state.set_channel_id(gid, "commands_channel_id", str(ctx.channel.id))
    await ctx.send(embed=_channel_set_embed(ctx.channel, "command menu"))
    await _post_commands_menu_if_needed()


# ── /setinteractivechannel — post the ephemeral interactive panel ─────────────

@tree.command(
    name="setinteractivechannel",
    description="Post the interactive panel to this channel (users click for ephemeral menus)",
)
@app_commands.default_permissions(manage_channels=True)
async def slash_setinteractivechannel(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    cid = str(interaction.channel_id)

    # Remove old panel if it exists
    old_msg_id = _get_interactive_panel_msg(gid, cid)
    ch = bot.get_channel(int(cid))
    if old_msg_id and ch:
        try:
            old = await ch.fetch_message(int(old_msg_id))
            await old.delete()
        except discord.HTTPException:
            pass

    panel_em = discord.Embed(
        title="⚽  Kickoff Bot — Interactive Panel",
        description=(
            "Use the buttons below to manage your personal preferences.\n"
            "All responses are **only visible to you**.\n\n"
            "🔔 **My Notifications** — toggle which events you receive\n"
            "⭐ **My Teams** — view your followed teams\n"
            "🏆 **My Competitions** — view your followed competitions\n"
            "🏅 **Leaderboard** — check the current standings\n"
            "📅 **Today's Matches** — see what's on today"
        ),
        color=0x5865F2,
    )
    panel_em.set_footer(text="Kickoff Bot  •  Responses are ephemeral — only you can see them")

    try:
        msg = await interaction.channel.send(embed=panel_em, view=InteractivePanel())
        _save_interactive_panel_msg(gid, cid, msg.id)
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"✅ Interactive panel posted in {interaction.channel.mention}.",
                color=emb.C_GREEN,
            ),
            ephemeral=True,
        )
    except discord.HTTPException as e:
        await interaction.response.send_message(
            embed=discord.Embed(description=f"❌ Failed: {e}", color=emb.C_RED),
            ephemeral=True,
        )


@bot.command(name="setinteractivechannel", help="Post the interactive panel to this channel")
@commands.has_permissions(manage_channels=True)
async def prefix_setinteractivechannel(ctx):
    gid = str(ctx.guild.id)
    cid = str(ctx.channel.id)
    old_msg_id = _get_interactive_panel_msg(gid, cid)
    if old_msg_id:
        try:
            old = await ctx.channel.fetch_message(int(old_msg_id))
            await old.delete()
        except discord.HTTPException:
            pass

    panel_em = discord.Embed(
        title="⚽  Kickoff Bot — Interactive Panel",
        description=(
            "Use the buttons below to manage your personal preferences.\n"
            "All responses are **only visible to you**."
        ),
        color=0x5865F2,
    )
    panel_em.set_footer(text="Kickoff Bot  •  Responses are ephemeral — only you can see them")
    msg = await ctx.send(embed=panel_em, view=InteractivePanel())
    _save_interactive_panel_msg(gid, cid, msg.id)
    await ctx.send(embed=discord.Embed(
        description=f"✅ Interactive panel posted in {ctx.channel.mention}.",
        color=emb.C_GREEN,
    ))


# ── /setmode  !setmode ────────────────────────────────────────────────────────

@tree.command(name="setmode", description="Set guild notification verbosity")
@app_commands.describe(mode="quiet | standard | detailed")
@app_commands.choices(mode=[
    app_commands.Choice(name="quiet — Goals + Red Cards + HT + FT only",         value="quiet"),
    app_commands.Choice(name="standard — + ET, PSO, MOTM, prediction polls",     value="standard"),
    app_commands.Choice(name="detailed — everything (lineups, recaps, tables)", value="detailed"),
])
@app_commands.default_permissions(manage_channels=True)
async def slash_setmode(interaction: discord.Interaction, mode: str = "standard"):
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    cfg["mode"] = mode
    state.set_guild_config(gid, cfg)
    icons = {"quiet": "🔇", "standard": "📢", "detailed": "📋"}
    await interaction.response.send_message(embed=discord.Embed(
        description=f"{icons.get(mode,'📢')} Guild notification mode set to **{mode}**.",
        color=emb.C_GREEN,
    ))


@bot.command(name="setmode", help="Set notification mode: quiet / standard / detailed")
@commands.has_permissions(manage_channels=True)
async def prefix_setmode(ctx, mode: str = "standard"):
    if mode not in ("quiet", "standard", "detailed"):
        await ctx.send(embed=discord.Embed(
            description="Mode must be `quiet`, `standard`, or `detailed`.", color=emb.C_RED
        ))
        return
    gid = str(ctx.guild.id)
    cfg = state.get_guild_config(gid)
    cfg["mode"] = mode
    state.set_guild_config(gid, cfg)
    icons = {"quiet": "🔇", "standard": "📢", "detailed": "📋"}
    await ctx.send(embed=discord.Embed(
        description=f"{icons.get(mode,'📢')} Mode set to **{mode}**.", color=emb.C_GREEN
    ))


# ── /settimezone  !settimezone ────────────────────────────────────────────────

@tree.command(name="settimezone", description="Set server timezone for daily summary")
@app_commands.describe(timezone_name="IANA timezone e.g. America/New_York, Europe/London")
@app_commands.default_permissions(manage_channels=True)
async def slash_settimezone(interaction: discord.Interaction, timezone_name: str):
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, KeyError):
        await interaction.response.send_message(embed=discord.Embed(
            description=f"❌ `{timezone_name}` is not a valid IANA timezone.\nExamples: `America/New_York` · `Europe/London` · `UTC`",
            color=emb.C_RED,
        ), ephemeral=True)
        return
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    cfg["timezone"] = timezone_name
    state.set_guild_config(gid, cfg)
    now_local = datetime.now(tz).strftime("%H:%M")
    await interaction.response.send_message(embed=discord.Embed(
        description=f"🕐 Timezone set to **{timezone_name}**  (current local time: {now_local})\nDaily summary sends at 23:50 in this timezone.",
        color=emb.C_GREEN,
    ))


@bot.command(name="settimezone", help="Set timezone. Usage: !settimezone America/New_York")
@commands.has_permissions(manage_channels=True)
async def prefix_settimezone(ctx, *, timezone_name: str = "UTC"):
    try:
        ZoneInfo(timezone_name)
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


# ── /timezone ─────────────────────────────────────────────────────────────────

@tree.command(name="timezone", description="Show current timezone for this server")
async def slash_timezone(interaction: discord.Interaction):
    cfg    = state.get_guild_config(str(interaction.guild_id))
    tz_str = cfg.get("timezone", "UTC")
    tz     = _tz_for(interaction.guild_id)
    await interaction.response.send_message(embed=discord.Embed(
        description=f"🕐 Current timezone: **{tz_str}**  (local time: {datetime.now(tz).strftime('%H:%M')})",
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

    def _ch(key: str) -> str:
        cid = cfg.get(key)
        if not cid:
            return "Not set"
        ch = guild.get_channel(int(cid))
        return ch.mention if ch else f"#{cid} (not found)"

    mode   = cfg.get("mode", "standard")
    tz_str = cfg.get("timezone", "UTC")

    mon_running = monitor_loop.is_running()
    if not cfg.get("channel_id"):
        mon_status = "⚠️ Disabled — run `/setchannel`"
    elif mon_running:
        mon_status = "✅ Active (every 2 min)"
    else:
        mon_status = "❌ Loop stopped"

    all_tracked = len(state._state.get("reminders_sent", {}))
    all_snaps   = len(state._state.get("snapshots", {}))
    user_prefs  = len(state._state.get("user_prefs", {}))
    following   = len(state._state.get("user_following", {}))

    em.add_field(name="📡 Live Matches",      value=_ch("channel_id"),             inline=True)
    em.add_field(name="🗳️ Predictions",      value=_ch("predictions_channel_id"), inline=True)
    em.add_field(name="🏆 Results",          value=_ch("results_channel_id"),     inline=True)
    em.add_field(name="📊 Summary",          value=_ch("summary_channel_id"),     inline=True)
    em.add_field(name="🎛️ Commands",         value=_ch("commands_channel_id"),    inline=True)
    em.add_field(name="\u200b", value="\u200b", inline=True)
    em.add_field(name="Mode",         value=f"`{mode}`",                inline=True)
    em.add_field(name="Timezone",     value=f"`{tz_str}`",              inline=True)
    em.add_field(name="Monitoring",   value=mon_status,                 inline=True)
    em.add_field(name="Tracked",
                 value=f"{all_tracked} matches  ·  {all_snaps} live snapshots",
                 inline=False)
    em.add_field(name="User data",
                 value=f"{user_prefs} custom notification profiles  ·  {following} users following teams/comps",
                 inline=False)
    em.set_footer(text="football-data.org • Kickoff Bot")
    return em


# ── /updatecommandsmenu ───────────────────────────────────────────────────────

@tree.command(name="updatecommandsmenu", description="Rebuild and repost the commands menu")
@app_commands.default_permissions(manage_channels=True)
async def slash_updatecommandsmenu(interaction: discord.Interaction):
    await _safe_defer(interaction)
    gid = str(interaction.guild_id)
    cfg = state.get_guild_config(gid)
    cid = cfg.get("commands_channel_id")

    if not cid:
        await interaction.followup.send(
            embed=discord.Embed(description="❌ No commands channel set — run `/setcommandschannel` first.", color=emb.C_RED)
        )
        return

    ch = bot.get_channel(int(cid))
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(
            embed=discord.Embed(description="❌ Commands channel not found.", color=emb.C_RED)
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
        await interaction.followup.send(
            embed=discord.Embed(description=f"✅ Commands menu updated in {ch.mention}.", color=emb.C_GREEN)
        )
    except discord.HTTPException as e:
        await interaction.followup.send(
            embed=discord.Embed(description=f"❌ Failed to post menu: {e}", color=emb.C_RED)
        )


@bot.command(name="updatecommandsmenu", help="Rebuild commands channel menu")
@commands.has_permissions(manage_channels=True)
async def prefix_updatecommandsmenu(ctx):
    gid = str(ctx.guild.id)
    cfg = state.get_guild_config(gid)
    cid = cfg.get("commands_channel_id")
    if not cid:
        await ctx.send(embed=discord.Embed(description="❌ No commands channel set.", color=emb.C_RED))
        return
    ch = bot.get_channel(int(cid))
    if ch:
        old_id = state.get_commands_menu_message(gid)
        if old_id:
            try:
                old = await ch.fetch_message(int(old_id))
                await old.delete()
            except discord.HTTPException:
                pass
        msg = await ch.send(embed=emb.embed_commands_menu(), view=ModePicker())
        state.save_commands_menu_message(gid, msg.id)
        await ctx.send(embed=discord.Embed(description=f"✅ Commands menu updated in {ch.mention}.", color=emb.C_GREEN))


# ── Per-user notification settings ───────────────────────────────────────────

@tree.command(name="mysettings", description="Configure your personal notification preferences (only you can see this)")
async def slash_mysettings(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    uid  = str(interaction.user.id)
    gid  = str(interaction.guild_id)
    view = UserSettingsView(uid, gid)
    await interaction.response.send_message(
        embed=_build_settings_embed(uid, gid),
        view=view,
        ephemeral=True,
    )


@tree.command(name="viewsettings", description="View your current notification settings (only you can see this)")
async def slash_viewsettings(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    uid  = str(interaction.user.id)
    gid  = str(interaction.guild_id)
    prefs = _get_user_prefs(uid, gid)
    if not prefs:
        await interaction.response.send_message(
            embed=discord.Embed(
                description="You have no custom settings — following the server's default mode.",
                color=emb.C_GREY,
            ),
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=_build_settings_embed(uid, gid),
        ephemeral=True,
    )


@tree.command(name="resetmysettings", description="Reset your notification settings to server defaults")
async def slash_resetmysettings(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    uid = str(interaction.user.id)
    gid = str(interaction.guild_id)
    _set_user_prefs(uid, gid, {})
    await interaction.response.send_message(
        embed=discord.Embed(
            description="✅ Your notification settings have been reset to server defaults.",
            color=emb.C_GREEN,
        ),
        ephemeral=True,
    )


# ── Team following ────────────────────────────────────────────────────────────

@tree.command(name="followteam", description="Follow a team — get notified when they play")
@app_commands.describe(name="Team name e.g. Brazil, Arsenal, Germany")
async def slash_followteam(interaction: discord.Interaction, name: str):
    uid      = str(interaction.user.id)
    following = dict(_get_user_following(uid))
    teams    = following.get("teams", [])

    # Normalise name
    clean = name.strip()
    if clean in teams:
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"⭐ You're already following **{clean}**.",
                color=emb.C_ORANGE,
            ),
            ephemeral=True,
        )
        return

    if len(teams) >= 20:
        await interaction.response.send_message(
            embed=discord.Embed(
                description="❌ You can follow up to **20 teams** at a time. Unfollow one first.",
                color=emb.C_RED,
            ),
            ephemeral=True,
        )
        return

    teams.append(clean)
    following["teams"] = teams
    _set_user_following(uid, following)

    await interaction.response.send_message(
        embed=discord.Embed(
            description=f"⭐ Now following **{clean}**!",
            color=emb.C_GREEN,
        ),
        ephemeral=True,
    )


@tree.command(name="unfollowteam", description="Unfollow a team")
@app_commands.describe(name="Team name to unfollow")
async def slash_unfollowteam(interaction: discord.Interaction, name: str):
    uid      = str(interaction.user.id)
    following = dict(_get_user_following(uid))
    teams    = following.get("teams", [])
    clean    = name.strip()

    if clean not in teams:
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"❌ You're not following **{clean}**.\nUse `/myteams` to see your list.",
                color=emb.C_RED,
            ),
            ephemeral=True,
        )
        return

    teams = [t for t in teams if t != clean]
    following["teams"] = teams
    _set_user_following(uid, following)

    await interaction.response.send_message(
        embed=discord.Embed(
            description=f"✅ Unfollowed **{clean}**.",
            color=emb.C_GREEN,
        ),
        ephemeral=True,
    )


@tree.command(name="myteams", description="See the teams you're following")
async def slash_myteams(interaction: discord.Interaction):
    uid  = str(interaction.user.id)
    view = UnfollowTeamView(uid)
    following = _get_user_following(uid)
    teams = following.get("teams", [])
    await interaction.response.send_message(
        embed=_build_teams_embed(uid),
        view=view if teams else None,
        ephemeral=True,
    )


# ── Competition following ─────────────────────────────────────────────────────

_COMP_CODES_UPPER = {c.upper() for c in COMPETITION_CODES} if COMPETITION_CODES else set()


@tree.command(name="followcompetition", description="Follow a competition — e.g. WC, PL, CL")
@app_commands.describe(code="Competition code e.g. WC, PL, CL, EURO")
async def slash_followcompetition(interaction: discord.Interaction, code: str):
    uid      = str(interaction.user.id)
    following = dict(_get_user_following(uid))
    comps    = following.get("competitions", [])
    clean    = code.upper().strip()

    if clean in comps:
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"🏆 You're already following **{clean}**.",
                color=emb.C_ORANGE,
            ),
            ephemeral=True,
        )
        return

    if len(comps) >= 15:
        await interaction.response.send_message(
            embed=discord.Embed(
                description="❌ You can follow up to **15 competitions** at a time.",
                color=emb.C_RED,
            ),
            ephemeral=True,
        )
        return

    comps.append(clean)
    following["competitions"] = comps
    _set_user_following(uid, following)

    await interaction.response.send_message(
        embed=discord.Embed(
            description=f"🏆 Now following **{clean}**!",
            color=emb.C_GREEN,
        ),
        ephemeral=True,
    )


@tree.command(name="unfollowcompetition", description="Unfollow a competition")
@app_commands.describe(code="Competition code to unfollow e.g. PL")
async def slash_unfollowcompetition(interaction: discord.Interaction, code: str):
    uid      = str(interaction.user.id)
    following = dict(_get_user_following(uid))
    comps    = following.get("competitions", [])
    clean    = code.upper().strip()

    if clean not in comps:
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"❌ You're not following **{clean}**.\nUse `/mycompetitions` to see your list.",
                color=emb.C_RED,
            ),
            ephemeral=True,
        )
        return

    comps = [c for c in comps if c != clean]
    following["competitions"] = comps
    _set_user_following(uid, following)

    await interaction.response.send_message(
        embed=discord.Embed(
            description=f"✅ Unfollowed **{clean}**.",
            color=emb.C_GREEN,
        ),
        ephemeral=True,
    )


@tree.command(name="mycompetitions", description="See the competitions you're following")
async def slash_mycompetitions(interaction: discord.Interaction):
    uid      = str(interaction.user.id)
    following = _get_user_following(uid)
    comps    = following.get("competitions", [])
    view     = UnfollowCompetitionView(uid) if comps else None
    await interaction.response.send_message(
        embed=_build_competitions_embed(uid),
        view=view,
        ephemeral=True,
    )


# ── /help  !help ──────────────────────────────────────────────────────────────

@tree.command(name="help", description="Show all available commands")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(embed=emb.embed_help(), ephemeral=True)


@bot.command(name="help", help="Show all available commands")
async def prefix_help(ctx):
    await ctx.send(embed=emb.embed_help())


# ── /testembed  (admin only) ──────────────────────────────────────────────────

_TEST_TYPES = (
    "reminder15", "kickoff", "lineup", "goal", "redcard",
    "halftime", "secondhalf", "extratime", "pso", "fulltime",
    "motm", "recap", "prediction",
)

# Dummy match/team data used for the prediction test
_TEST_MATCH_ID   = "test-0000"
_TEST_HOME_TEAM  = "Home FC"
_TEST_AWAY_TEAM  = "Away United"


@tree.command(name="testembed", description="[Admin] Preview a notification embed with test data")
@app_commands.describe(embed_type="Type of embed to preview")
@app_commands.choices(embed_type=[app_commands.Choice(name=t, value=t) for t in _TEST_TYPES])
@app_commands.default_permissions(administrator=True)
async def slash_testembed(interaction: discord.Interaction, embed_type: str):
    # FIX: for "prediction" type, also attach the PredictionView so score buttons work
    if embed_type == "prediction":
        view = PredictionView(_TEST_MATCH_ID, _TEST_HOME_TEAM, _TEST_AWAY_TEAM, knockout=False)
        await interaction.response.send_message(
            embed=emb.embed_test(embed_type),
            view=view,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            embed=emb.embed_test(embed_type),
            ephemeral=True,
        )


@bot.command(name="testembed", help="[Admin] Test embed. Usage: !testembed goal")
@commands.has_permissions(administrator=True)
async def prefix_testembed(ctx, embed_type: str = "goal"):
    if embed_type == "prediction":
        view = PredictionView(_TEST_MATCH_ID, _TEST_HOME_TEAM, _TEST_AWAY_TEAM, knockout=False)
        await ctx.send(embed=emb.embed_test(embed_type), view=view)
    else:
        await ctx.send(embed=emb.embed_test(embed_type))


# ══════════════════════════════════════════════════════════════════════════════
#  ERROR HANDLING
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=discord.Embed(
            description="🔒 You need the **Manage Channels** permission.", color=emb.C_RED
        ))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=discord.Embed(
            description="⚠️ Missing argument — see `!help`.", color=emb.C_ORANGE
        ))
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
        if interaction.response.is_done():
            await interaction.followup.send(
                embed=discord.Embed(description=msg, color=emb.C_RED), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=discord.Embed(description=msg, color=emb.C_RED), ephemeral=True
            )
    except discord.HTTPException:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
