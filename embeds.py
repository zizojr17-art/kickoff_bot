"""
embeds.py — World Cup 2026 themed Discord embed builders.
All public functions return discord.Embed (or list[discord.Embed]).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord

from football_api import (
    get_current_score,
    get_score,
    is_knockout,
    parse_dt,
    team_display,
    team_flag,
    STAGE_NAMES,
)

# ── Brand palette ─────────────────────────────────────────────────────────────

C_GOLD    = 0xD4AF37   # World Cup gold — primary brand
C_BLUE    = 0x0A3161   # WC deep blue — header accents
C_RED     = 0xE53935   # errors / red cards
C_GREEN   = 0x2ECC71   # success / goals
C_ORANGE  = 0xF39C12   # warnings / extra time
C_PURPLE  = 0x8E44AD   # predictions / leaderboard
C_GREY    = 0x95A5A6   # neutral / no data
C_TEAL    = 0x1ABC9C   # standings / schedule

WC_FOOTER = "🏆 FIFA World Cup 2026  •  football-data.org"
WC_ICON   = "https://upload.wikimedia.org/wikipedia/en/thumb/4/43/FIFA_World_Cup_2026_logo.svg/120px-FIFA_World_Cup_2026_logo.svg.png"


def _footer(em: discord.Embed, extra: str = "") -> discord.Embed:
    text = f"{WC_FOOTER}  {extra}".strip() if extra else WC_FOOTER
    em.set_footer(text=text)
    return em


def _ts(dt: datetime | None) -> str:
    if dt is None:
        return "TBD"
    return f"<t:{int(dt.timestamp())}:F>"


def _ts_rel(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return f"<t:{int(dt.timestamp())}:R>"


def _match_title(match: dict) -> str:
    home = team_display(match.get("homeTeam", {}))
    away = team_display(match.get("awayTeam", {}))
    hf   = team_flag(match.get("homeTeam", {}))
    af   = team_flag(match.get("awayTeam", {}))
    return f"{hf} {home}  vs  {away} {af}"


def _stage_badge(match: dict) -> str:
    stage = match.get("stage", "")
    group = match.get("group") or ""
    group_letter = group.replace("GROUP_", "") if group else ""
    label = STAGE_NAMES.get(stage, stage.replace("_", " ").title())
    if group_letter:
        label += f" · Group {group_letter}"
    return label


# ── Startup ────────────────────────────────────────────────────────────────────

def embed_startup(bot_user: Any) -> discord.Embed:
    em = discord.Embed(
        title="⚽ World Cup 2026 Bot Online",
        description=(
            "Live match monitoring is active.\n"
            "Use `/setup` to configure channels, or `/help` for all commands."
        ),
        color=C_GOLD,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_thumbnail(url=WC_ICON)
    if bot_user:
        em.set_author(name=str(bot_user), icon_url=getattr(bot_user, "display_avatar", None) and str(bot_user.display_avatar))
    return _footer(em, "• Started")


# ── Today / schedule ──────────────────────────────────────────────────────────

def embed_today(matches: list[dict]) -> list[discord.Embed]:
    if not matches:
        em = discord.Embed(
            title="📅 Today's World Cup Matches",
            description="No World Cup matches scheduled today.\nCheck `/upcoming` for the next fixtures.",
            color=C_GREY,
        )
        return [_footer(em)]

    embeds: list[discord.Embed] = []
    chunk_size = 8
    for i in range(0, len(matches), chunk_size):
        chunk = matches[i:i + chunk_size]
        page  = (i // chunk_size) + 1
        total = (len(matches) - 1) // chunk_size + 1
        em    = discord.Embed(
            title=f"📅 Today's Matches" + (f"  ({page}/{total})" if total > 1 else ""),
            color=C_TEAL,
        )
        for m in chunk:
            home   = team_display(m.get("homeTeam", {}))
            away   = team_display(m.get("awayTeam", {}))
            hf     = team_flag(m.get("homeTeam", {}))
            af     = team_flag(m.get("awayTeam", {}))
            status = m.get("status", "")
            dt     = parse_dt(m.get("utcDate", ""))

            if status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
                h, a  = get_current_score(m)
                label = f"🔴 **LIVE**  {h}–{a}"
            elif status == "FINISHED":
                h, a  = get_score(m, "fullTime")
                label = f"✅ FT  {h}–{a}"
            else:
                label = _ts(dt) + (f"  {_ts_rel(dt)}" if dt else "")

            stage = _stage_badge(m)
            em.add_field(
                name=f"{hf} {home}  vs  {away} {af}",
                value=f"{label}\n*{stage}*",
                inline=False,
            )
        embeds.append(_footer(em))
    return embeds


def embed_upcoming(matches: list[dict], days: int) -> discord.Embed:
    em = discord.Embed(
        title=f"📆 Upcoming World Cup Fixtures — Next {days} Days",
        color=C_TEAL,
    )
    if not matches:
        em.description = "No fixtures found in this window.\nTry `/upcoming 30` to look further ahead."
        return _footer(em)

    for m in matches[:20]:
        home = team_display(m.get("homeTeam", {}))
        away = team_display(m.get("awayTeam", {}))
        hf   = team_flag(m.get("homeTeam", {}))
        af   = team_flag(m.get("awayTeam", {}))
        dt   = parse_dt(m.get("utcDate", ""))
        stage = _stage_badge(m)
        em.add_field(
            name=f"{hf} {home}  vs  {away} {af}",
            value=f"{_ts(dt)}  {_ts_rel(dt)}\n*{stage}*",
            inline=False,
        )
    if len(matches) > 20:
        em.set_footer(text=f"Showing 20 of {len(matches)} fixtures  •  {WC_FOOTER}")
    else:
        _footer(em)
    return em


# ── Live matches ──────────────────────────────────────────────────────────────

def embed_live(matches: list[dict]) -> discord.Embed:
    em = discord.Embed(
        title="🔴 Live World Cup Matches",
        color=C_RED,
        timestamp=datetime.now(timezone.utc),
    )
    if not matches:
        em.description = (
            "No matches are live right now.\n"
            "Use `/today` to see today's schedule, or `/nextmatch` for the next kick-off."
        )
        em.color = C_GREY
        return _footer(em)

    for m in matches:
        home = team_display(m.get("homeTeam", {}))
        away = team_display(m.get("awayTeam", {}))
        hf   = team_flag(m.get("homeTeam", {}))
        af   = team_flag(m.get("awayTeam", {}))
        h, a = get_current_score(m)

        status = m.get("status", "")
        minute = m.get("minute") or ""
        status_map = {
            "IN_PLAY":           f"⚽ {minute}'" if minute else "⚽ In Play",
            "PAUSED":            "⏸️ Half Time",
            "EXTRA_TIME":        f"⚡ Extra Time {minute}'" if minute else "⚡ Extra Time",
            "PENALTY_SHOOTOUT":  "🎯 Penalty Shootout",
        }
        badge = status_map.get(status, "🔴 Live")
        stage = _stage_badge(m)

        em.add_field(
            name=f"{hf} {home}  {h} – {a}  {away} {af}",
            value=f"**{badge}**  ·  *{stage}*",
            inline=False,
        )
    return _footer(em, "• Updates every 2 min")


# ── Next match ────────────────────────────────────────────────────────────────

def embed_nextmatch(match: dict) -> discord.Embed:
    home  = team_display(match.get("homeTeam", {}))
    away  = team_display(match.get("awayTeam", {}))
    hf    = team_flag(match.get("homeTeam", {}))
    af    = team_flag(match.get("awayTeam", {}))
    dt    = parse_dt(match.get("utcDate", ""))
    stage = _stage_badge(match)

    em = discord.Embed(
        title=f"⏰ Next Match  ·  {stage}",
        description=f"## {hf} {home}  vs  {away} {af}",
        color=C_GOLD,
    )
    em.add_field(name="📅 Kick-off", value=f"{_ts(dt)}\n{_ts_rel(dt)}", inline=True)

    venue = match.get("venue") or ""
    if venue:
        em.add_field(name="🏟️ Venue", value=venue, inline=True)

    home_crest = (match.get("homeTeam") or {}).get("crest")
    if home_crest:
        em.set_thumbnail(url=home_crest)

    return _footer(em)


# ── Match detail ──────────────────────────────────────────────────────────────

def embed_kickoff(match: dict, detail: dict) -> discord.Embed:
    home  = team_display(match.get("homeTeam", {}))
    away  = team_display(match.get("awayTeam", {}))
    hf    = team_flag(match.get("homeTeam", {}))
    af    = team_flag(match.get("awayTeam", {}))
    stage = _stage_badge(match)

    em = discord.Embed(
        title="🔔 Kick-off!",
        description=f"# {hf} {home}  vs  {away} {af}\n*{stage}*",
        color=C_GOLD,
        timestamp=datetime.now(timezone.utc),
    )
    home_crest = (match.get("homeTeam") or {}).get("crest")
    if home_crest:
        em.set_thumbnail(url=home_crest)
    return _footer(em, "• The game is underway!")


def embed_halftime(match: dict, detail: dict) -> discord.Embed:
    home = team_display(match.get("homeTeam", {}))
    away = team_display(match.get("awayTeam", {}))
    hf   = team_flag(match.get("homeTeam", {}))
    af   = team_flag(match.get("awayTeam", {}))
    h, a = get_score(detail, "halfTime") if detail else get_current_score(match)

    em = discord.Embed(
        title="⏱️ Half Time",
        description=f"## {hf} {home}  **{h} – {a}**  {away} {af}",
        color=C_ORANGE,
        timestamp=datetime.now(timezone.utc),
    )
    goals = (detail or {}).get("goals", [])
    if goals:
        scorers = []
        for g in goals:
            scorer = (g.get("scorer") or {}).get("name", "OG")
            minute = g.get("minute", "?")
            team   = (g.get("team") or {}).get("shortName", "")
            scorers.append(f"⚽ **{scorer}** {minute}' *({team})*")
        em.add_field(name="Goalscorers", value="\n".join(scorers), inline=False)
    return _footer(em)


def embed_second_half(match: dict) -> discord.Embed:
    home = team_display(match.get("homeTeam", {}))
    away = team_display(match.get("awayTeam", {}))
    hf   = team_flag(match.get("homeTeam", {}))
    af   = team_flag(match.get("awayTeam", {}))
    h, a = get_current_score(match)

    em = discord.Embed(
        title="▶️ Second Half Underway",
        description=f"{hf} {home}  **{h} – {a}**  {away} {af}",
        color=C_TEAL,
        timestamp=datetime.now(timezone.utc),
    )
    return _footer(em)


def embed_extra_time(match: dict, detail: dict) -> discord.Embed:
    home = team_display(match.get("homeTeam", {}))
    away = team_display(match.get("awayTeam", {}))
    hf   = team_flag(match.get("homeTeam", {}))
    af   = team_flag(match.get("awayTeam", {}))
    h, a = get_current_score(match)

    em = discord.Embed(
        title="⚡ Extra Time!",
        description=f"## {hf} {home}  **{h} – {a}**  {away} {af}\nKnockout match — it's going to extra time!",
        color=C_ORANGE,
        timestamp=datetime.now(timezone.utc),
    )
    return _footer(em)


def embed_penalty_shootout(match: dict, detail: dict) -> discord.Embed:
    home = team_display(match.get("homeTeam", {}))
    away = team_display(match.get("awayTeam", {}))
    hf   = team_flag(match.get("homeTeam", {}))
    af   = team_flag(match.get("awayTeam", {}))

    em = discord.Embed(
        title="🎯 Penalty Shootout!",
        description=(
            f"## {hf} {home}  vs  {away} {af}\n"
            "It all comes down to penalties! 🍿"
        ),
        color=C_PURPLE,
        timestamp=datetime.now(timezone.utc),
    )
    return _footer(em)


def embed_goal(match: dict, detail: dict, goal: dict) -> discord.Embed:
    home = team_display(match.get("homeTeam", {}))
    away = team_display(match.get("awayTeam", {}))
    hf   = team_flag(match.get("homeTeam", {}))
    af   = team_flag(match.get("awayTeam", {}))
    h, a = get_current_score(match)

    scorer  = (goal.get("scorer") or {}).get("name", "Unknown")
    assist  = (goal.get("assist") or {}).get("name")
    minute  = goal.get("minute", "?")
    goal_t  = goal.get("type", "")
    team_id = (goal.get("team") or {}).get("id")
    home_id = (match.get("homeTeam") or {}).get("id")

    scorer_team = home if team_id == home_id else away
    flag        = hf if team_id == home_id else af

    penalty_note = " *(Pen.)*" if goal_t == "PENALTY" else ""
    og_note      = " *(O.G.)*" if goal_t == "OWN"     else ""

    em = discord.Embed(
        title=f"⚽ GOAL! — {flag} {scorer_team}",
        description=(
            f"## {hf} {home}  **{h} – {a}**  {away} {af}\n\n"
            f"**{scorer}**{penalty_note}{og_note}  ·  {minute}'"
        ),
        color=C_GREEN,
        timestamp=datetime.now(timezone.utc),
    )
    if assist:
        em.add_field(name="🎯 Assist", value=assist, inline=True)
    em.add_field(name="⏱️ Minute", value=f"{minute}'", inline=True)

    home_crest = (match.get("homeTeam") or {}).get("crest")
    away_crest = (match.get("awayTeam") or {}).get("crest")
    scoring_crest = home_crest if team_id == home_id else away_crest
    if scoring_crest:
        em.set_thumbnail(url=scoring_crest)

    return _footer(em, "• Goal alert")


def embed_red_card(match: dict, detail: dict, card: dict) -> discord.Embed:
    home   = team_display(match.get("homeTeam", {}))
    away   = team_display(match.get("awayTeam", {}))
    hf     = team_flag(match.get("homeTeam", {}))
    af     = team_flag(match.get("awayTeam", {}))
    player = (card.get("player") or {}).get("name", "Unknown")
    minute = card.get("minute", "?")
    reason = card.get("reason") or ""
    team   = (card.get("team") or {}).get("shortName", "")

    card_type = card.get("card", "RED")
    title     = "🟥 Red Card" if card_type == "RED" else "🟨🟥 Second Yellow — Red Card"

    em = discord.Embed(
        title=title,
        description=(
            f"**{player}** has been sent off!  ·  {minute}'\n"
            f"*{team}*"
        ),
        color=C_RED,
        timestamp=datetime.now(timezone.utc),
    )
    em.add_field(name="Match", value=f"{hf} {home}  vs  {away} {af}", inline=False)
    if reason:
        em.add_field(name="Reason", value=reason, inline=True)
    return _footer(em)


def embed_fulltime(match: dict, detail: dict) -> discord.Embed:
    home   = team_display(match.get("homeTeam", {}))
    away   = team_display(match.get("awayTeam", {}))
    hf     = team_flag(match.get("homeTeam", {}))
    af     = team_flag(match.get("awayTeam", {}))
    h, a   = get_score(detail, "fullTime")
    stage  = _stage_badge(match)

    if h is None:
        h, a = get_current_score(match)

    em = discord.Embed(
        title="🏁 Full Time",
        description=(
            f"## {hf} {home}  **{h} – {a}**  {away} {af}\n"
            f"*{stage}*"
        ),
        color=C_GOLD,
        timestamp=datetime.now(timezone.utc),
    )

    goals = (detail or {}).get("goals", [])
    if goals:
        home_id = (match.get("homeTeam") or {}).get("id")
        home_g, away_g = [], []
        for g in goals:
            tid    = (g.get("team") or {}).get("id")
            scorer = (g.get("scorer") or {}).get("name", "OG")
            minute = g.get("minute", "?")
            gt     = g.get("type", "")
            tag    = " *(Pen)*" if gt == "PENALTY" else (" *(OG)*" if gt == "OWN" else "")
            entry  = f"⚽ {scorer}{tag}  {minute}'"
            if tid == home_id:
                home_g.append(entry)
            else:
                away_g.append(entry)

        if home_g:
            em.add_field(name=f"{hf} {home}", value="\n".join(home_g), inline=True)
        if away_g:
            em.add_field(name=f"{af} {away}", value="\n".join(away_g), inline=True)

    home_crest = (match.get("homeTeam") or {}).get("crest")
    if home_crest:
        em.set_thumbnail(url=home_crest)

    return _footer(em)


# ── Lineups ───────────────────────────────────────────────────────────────────

def embed_lineups(match: dict, detail: dict) -> discord.Embed:
    home  = team_display(match.get("homeTeam", {}))
    away  = team_display(match.get("awayTeam", {}))
    hf    = team_flag(match.get("homeTeam", {}))
    af    = team_flag(match.get("awayTeam", {}))
    stage = _stage_badge(match)

    em = discord.Embed(
        title=f"📋 Confirmed Lineups  ·  {stage}",
        description=f"**{hf} {home}  vs  {away} {af}**",
        color=C_BLUE,
    )

    lineups = (detail or {}).get("lineups", [])
    for lineup in lineups[:2]:
        team_name = (lineup.get("team") or {}).get("shortName", "Team")
        formation = lineup.get("formation", "")
        players   = lineup.get("startXI", [])
        player_names = []
        for entry in players:
            p    = entry.get("player") or entry
            name = p.get("name", "Unknown")
            pos  = p.get("position", "")
            pos_map = {"GK": "🟡", "DEF": "🔵", "MID": "🟢", "FWD": "🔴"}
            icon = pos_map.get(pos, "⚪")
            player_names.append(f"{icon} {name}")
        title = f"{team_name}" + (f"  [{formation}]" if formation else "")
        em.add_field(name=title, value="\n".join(player_names) or "TBD", inline=True)

    return _footer(em)


# ── Reminder ──────────────────────────────────────────────────────────────────

def embed_reminder(match: dict, minutes: int) -> discord.Embed:
    home  = team_display(match.get("homeTeam", {}))
    away  = team_display(match.get("awayTeam", {}))
    hf    = team_flag(match.get("homeTeam", {}))
    af    = team_flag(match.get("awayTeam", {}))
    dt    = parse_dt(match.get("utcDate", ""))
    stage = _stage_badge(match)

    em = discord.Embed(
        title=f"⏰ Kick-off in {minutes} minute{'s' if minutes != 1 else ''}",
        description=(
            f"## {hf} {home}  vs  {away} {af}\n"
            f"*{stage}*\n\n"
            f"📅 {_ts(dt)}"
        ),
        color=C_GOLD,
    )
    em.add_field(
        name="🎯 Make your prediction!",
        value="Use `/predict` or check the predictions channel to lock in your score.",
        inline=False,
    )
    home_crest = (match.get("homeTeam") or {}).get("crest")
    if home_crest:
        em.set_thumbnail(url=home_crest)
    return _footer(em)


# ── Predictions ───────────────────────────────────────────────────────────────

def embed_prediction_poll(match: dict) -> discord.Embed:
    home  = team_display(match.get("homeTeam", {}))
    away  = team_display(match.get("awayTeam", {}))
    hf    = team_flag(match.get("homeTeam", {}))
    af    = team_flag(match.get("awayTeam", {}))
    dt    = parse_dt(match.get("utcDate", ""))
    ko    = is_knockout(match)
    stage = _stage_badge(match)

    em = discord.Embed(
        title="🎯 Score Prediction",
        description=(
            f"## {hf} {home}  vs  {away} {af}\n"
            f"*{stage}*\n\n"
            f"📅 Kick-off: {_ts(dt)}"
        ),
        color=C_PURPLE,
    )
    em.add_field(name="Scoring", value="🥇 Exact score = **3 pts**\n✅ Correct result = **1 pt**", inline=True)
    if ko:
        em.add_field(name="⚠️ Knockout Rule", value="Draws not allowed — pick a winner!", inline=True)
    em.add_field(
        name="How to predict",
        value="Click **🎯 Predict Score** below to open your private score picker.",
        inline=False,
    )
    home_crest = (match.get("homeTeam") or {}).get("crest")
    if home_crest:
        em.set_thumbnail(url=home_crest)
    return _footer(em, "• Predictions lock at kick-off")


def embed_prediction_results(
    match: dict,
    actual_h: int,
    actual_a: int,
    exact_winners: list[tuple],
    result_winners: list[tuple],
) -> discord.Embed:
    home  = team_display(match.get("homeTeam", {}))
    away  = team_display(match.get("awayTeam", {}))
    hf    = team_flag(match.get("homeTeam", {}))
    af    = team_flag(match.get("awayTeam", {}))

    em = discord.Embed(
        title="📊 Prediction Results",
        description=f"## {hf} {home}  **{actual_h} – {actual_a}**  {away} {af}",
        color=C_PURPLE,
    )

    if exact_winners:
        winners_txt = "\n".join(f"🥇 <@{uid}>" for uid, _, _ in exact_winners[:10])
        em.add_field(name=f"🎯 Exact Score! (+3 pts)  [{len(exact_winners)}]", value=winners_txt, inline=False)

    if result_winners:
        r_txt = "\n".join(f"✅ <@{uid}>" for uid, _, _ in result_winners[:10])
        em.add_field(name=f"✅ Correct Result (+1 pt)  [{len(result_winners)}]", value=r_txt, inline=False)

    if not exact_winners and not result_winners:
        em.add_field(name="😅 Nobody got it!", value="Better luck next match.", inline=False)

    return _footer(em)


# ── MOTM ──────────────────────────────────────────────────────────────────────

def embed_motm_vote(match: dict) -> discord.Embed:
    home  = team_display(match.get("homeTeam", {}))
    away  = team_display(match.get("awayTeam", {}))
    hf    = team_flag(match.get("homeTeam", {}))
    af    = team_flag(match.get("awayTeam", {}))

    em = discord.Embed(
        title="🌟 Man of the Match Vote",
        description=(
            f"**{hf} {home}  vs  {away} {af}**\n\n"
            "Who was the standout player? Cast your vote below!\n"
            "*You can change your vote until full time.*"
        ),
        color=C_GOLD,
    )
    return _footer(em, "• Voting closes at full time")


def embed_motm_result(match: dict, winners: list[str], tally: dict) -> discord.Embed:
    home = team_display(match.get("homeTeam", {}))
    away = team_display(match.get("awayTeam", {}))
    hf   = team_flag(match.get("homeTeam", {}))
    af   = team_flag(match.get("awayTeam", {}))

    em = discord.Embed(
        title="🏆 Man of the Match",
        description=f"**{hf} {home}  vs  {away} {af}**",
        color=C_GOLD,
    )

    em.add_field(
        name="🌟 Winner" + ("s" if len(winners) > 1 else ""),
        value="\n".join(f"⭐ **{w}**" for w in winners),
        inline=False,
    )

    if tally:
        sorted_tally = sorted(tally.items(), key=lambda x: x[1], reverse=True)[:5]
        vote_lines   = [f"`{v:>3} votes`  {p}" for p, v in sorted_tally]
        em.add_field(name="Full vote breakdown", value="\n".join(vote_lines), inline=False)

    return _footer(em)


# ── Leaderboard ───────────────────────────────────────────────────────────────

def embed_leaderboard(guild: Any, lb: dict, title_prefix: str = "") -> discord.Embed:
    em = discord.Embed(
        title=f"{title_prefix}🏅 Prediction Leaderboard" if title_prefix else "🏅 Prediction Leaderboard",
        color=C_GOLD,
        timestamp=datetime.now(timezone.utc),
    )
    if guild:
        em.set_author(name=guild.name, icon_url=guild.icon and str(guild.icon))

    if not lb:
        em.description = "No predictions yet — be the first to `/predict`!"
        return _footer(em)

    sorted_lb  = sorted(lb.items(), key=lambda x: x[1].get("points", 0), reverse=True)
    medals     = ["🥇", "🥈", "🥉"]
    rows       = []
    for rank, (uid, entry) in enumerate(sorted_lb[:15], 1):
        medal  = medals[rank - 1] if rank <= 3 else f"`{rank}.`"
        pts    = entry.get("points", 0)
        exact  = entry.get("exact", 0)
        streak = entry.get("streak", 0)
        streak_str = f"  🔥{streak}" if streak >= 3 else ""
        rows.append(f"{medal} <@{uid}>  **{pts} pts**  *(⭐{exact} exact{streak_str})*")

    em.description = "\n".join(rows)
    em.set_footer(text=f"🥇 Exact = 3 pts  ✅ Result = 1 pt  •  {WC_FOOTER}")
    return em


def embed_monthly_leaderboard(guild: Any, data: list[tuple], month_label: str) -> discord.Embed:
    em = discord.Embed(
        title=f"📅 Monthly Leaderboard — {month_label}",
        color=C_PURPLE,
        timestamp=datetime.now(timezone.utc),
    )
    if guild:
        em.set_author(name=guild.name, icon_url=guild.icon and str(guild.icon))

    if not data:
        em.description = f"No predictions recorded for {month_label} yet."
        return _footer(em)

    medals = ["🥇", "🥈", "🥉"]
    rows   = []
    for rank, (uid, pts) in enumerate(data[:15], 1):
        medal = medals[rank - 1] if rank <= 3 else f"`{rank}.`"
        rows.append(f"{medal} <@{uid}>  **{pts} pts**")

    em.description = "\n".join(rows)
    return _footer(em)


def embed_prediction_stats(user: Any, stats: dict, guild_rank: int | None) -> discord.Embed:
    em = discord.Embed(
        title=f"📊 Prediction Stats — {user.display_name}",
        color=C_PURPLE,
    )
    em.set_thumbnail(url=str(user.display_avatar))

    total    = stats["total"]
    exact    = stats["exact"]
    correct  = stats["correct"]
    accuracy = stats["accuracy"]
    streak   = stats["streak"]
    best     = stats["best_streak"]

    em.add_field(name="🎯 Total Predictions", value=str(total), inline=True)
    em.add_field(name="⭐ Exact Scores",       value=str(exact), inline=True)
    em.add_field(name="✅ Correct Results",    value=str(correct), inline=True)
    em.add_field(name="🎲 Accuracy",           value=f"{accuracy}%", inline=True)
    em.add_field(name="🔥 Current Streak",     value=str(streak), inline=True)
    em.add_field(name="🏆 Best Streak",        value=str(best), inline=True)
    em.add_field(name="💰 Total Points",       value=str(stats["points"]), inline=True)

    if guild_rank:
        em.add_field(name="🏅 Server Rank", value=f"#{guild_rank}", inline=True)

    monthly = stats.get("monthly", {})
    if monthly:
        month_rows = []
        for mk, pts in sorted(monthly.items(), reverse=True)[:3]:
            try:
                dt_m  = datetime.strptime(mk, "%Y-%m")
                label = dt_m.strftime("%B %Y")
            except ValueError:
                label = mk
            month_rows.append(f"**{label}**: {pts} pts")
        em.add_field(name="📅 Monthly Breakdown", value="\n".join(month_rows), inline=False)

    return _footer(em)


# ── Standings ─────────────────────────────────────────────────────────────────

def embed_standings(data: dict) -> list[discord.Embed]:
    tables = data.get("standings", [])
    if not tables:
        em = discord.Embed(
            title="🏆 World Cup 2026 Standings",
            description=(
                "Standings will appear once the group stage begins.\n"
                "Check back when the tournament kicks off!"
            ),
            color=C_GREY,
        )
        return [_footer(em)]

    embeds = []
    for block in tables:
        group_raw = block.get("group", "")
        letter    = group_raw.replace("GROUP_", "") if group_raw else "?"
        rows      = block.get("table", [])
        em        = embed_wc_group(letter, rows)
        embeds.append(em)
    return embeds


def embed_wc_group(letter: str, rows: list[dict]) -> discord.Embed:
    em = discord.Embed(
        title=f"🏆 Group {letter}",
        color=C_GOLD,
    )

    if not rows:
        em.description = "No standings data yet."
        return _footer(em)

    header = "`#  Team                  P   W  D  L  GF GA GD  Pts`"
    lines  = [header]
    for entry in rows:
        pos    = entry.get("position", "?")
        team   = (entry.get("team") or {})
        name   = team_display(team)
        flag   = team_flag(team)
        played = entry.get("playedGames", 0)
        won    = entry.get("won", 0)
        draw   = entry.get("draw", 0)
        lost   = entry.get("lost", 0)
        gf     = entry.get("goalsFor", 0)
        ga     = entry.get("goalsAgainst", 0)
        gd     = entry.get("goalDifference", 0)
        pts    = entry.get("points", 0)
        q      = "✅ " if pos <= 2 else "   "
        lines.append(
            f"`{q}{pos:<2} {flag} {name:<18} {played:<3} {won:<2} {draw:<2} {lost:<2} "
            f"{gf:<3}{ga:<3}{gd:+<3} {pts:>3}`"
        )

    em.description = "\n".join(lines)
    em.set_footer(text=f"✅ = Advancing  •  {WC_FOOTER}")
    return em


def embed_wc_group_update(letter: str, rows: list[dict]) -> discord.Embed:
    em = embed_wc_group(letter, rows)
    em.title = f"📊 Group {letter} Update"
    return em


def embed_worldcup_overview(data: dict) -> list[discord.Embed]:
    tables = data.get("standings", [])
    if not tables:
        em = discord.Embed(
            title="🏆 FIFA World Cup 2026 — Overview",
            description="Group stage standings will appear once the tournament begins.",
            color=C_GREY,
        )
        return [_footer(em)]
    return [embed_wc_group(block.get("group", "?").replace("GROUP_", ""), block.get("table", [])) for block in tables]


# ── Bracket ───────────────────────────────────────────────────────────────────

def embed_bracket(knockout_matches: list[dict]) -> list[discord.Embed]:
    if not knockout_matches:
        em = discord.Embed(
            title="🏆 World Cup 2026 — Knockout Bracket",
            description=(
                "Knockout bracket will appear once the group stage is complete.\n"
                "Use `/standings` to track group progress."
            ),
            color=C_GREY,
        )
        return [_footer(em)]

    stages_order = ["LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL"]
    by_stage: dict[str, list] = {s: [] for s in stages_order}
    for m in knockout_matches:
        s = m.get("stage", "")
        if s in by_stage:
            by_stage[s].append(m)

    embeds = []
    for stage in stages_order:
        matches = by_stage[stage]
        if not matches:
            continue
        em = discord.Embed(
            title=f"🏆 {STAGE_NAMES.get(stage, stage)}",
            color=C_GOLD,
        )
        for m in matches:
            home = team_display(m.get("homeTeam", {}))
            away = team_display(m.get("awayTeam", {}))
            hf   = team_flag(m.get("homeTeam", {}))
            af   = team_flag(m.get("awayTeam", {}))
            dt   = parse_dt(m.get("utcDate", ""))
            stat = m.get("status", "")
            if stat == "FINISHED":
                h, a  = get_score(m, "fullTime")
                score = f"**{h} – {a}** ✅"
            elif stat in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
                h, a  = get_current_score(m)
                score = f"**{h} – {a}** 🔴"
            else:
                score = _ts(dt)
            em.add_field(
                name=f"{hf} {home}  vs  {away} {af}",
                value=score,
                inline=False,
            )
        embeds.append(_footer(em))
    return embeds


# ── Match center ──────────────────────────────────────────────────────────────

def embed_matchcenter(
    live: list[dict],
    next_match: dict | None,
    standings_snap: list[dict],
    lb: dict,
    guild: Any,
) -> list[discord.Embed]:
    embeds = []

    # — Live or next match —
    em1 = discord.Embed(title="⚽ Match Center", color=C_GOLD)
    em1.set_thumbnail(url=WC_ICON)

    if live:
        em1.add_field(name="🔴 Live Now", value=f"{len(live)} match{'es' if len(live) > 1 else ''} in progress", inline=False)
        for m in live[:3]:
            home = team_display(m.get("homeTeam", {}))
            away = team_display(m.get("awayTeam", {}))
            hf   = team_flag(m.get("homeTeam", {}))
            af   = team_flag(m.get("awayTeam", {}))
            h, a = get_current_score(m)
            em1.add_field(name=f"{hf} {home}  {h}–{a}  {away} {af}", value="\u200b", inline=False)
    elif next_match:
        home  = team_display(next_match.get("homeTeam", {}))
        away  = team_display(next_match.get("awayTeam", {}))
        hf    = team_flag(next_match.get("homeTeam", {}))
        af    = team_flag(next_match.get("awayTeam", {}))
        dt    = parse_dt(next_match.get("utcDate", ""))
        stage = _stage_badge(next_match)
        em1.add_field(
            name="⏰ Next Match",
            value=f"{hf} {home}  vs  {away} {af}\n*{stage}*\n{_ts(dt)}  {_ts_rel(dt)}",
            inline=False,
        )
    else:
        em1.add_field(name="📭 No Upcoming Matches", value="Check back later.", inline=False)

    _footer(em1)
    embeds.append(em1)

    # — Standings snapshot (first group) —
    if standings_snap:
        first = standings_snap[0]
        g     = first.get("group", "?").replace("GROUP_", "")
        rows  = first.get("table", [])[:4]
        em2   = discord.Embed(title=f"🏆 Standings Snapshot — Group {g}", color=C_TEAL)
        lines = []
        for entry in rows:
            pos  = entry.get("position", "?")
            team = entry.get("team", {})
            name = team_display(team)
            flag = team_flag(team)
            pts  = entry.get("points", 0)
            gd   = entry.get("goalDifference", 0)
            q    = "✅" if pos <= 2 else "  "
            lines.append(f"`{q}{pos}. {flag} {name:<20} {pts:>3} pts  {gd:+d} GD`")
        em2.description = "\n".join(lines)
        _footer(em2)
        embeds.append(em2)

    # — Leaderboard snapshot —
    if lb:
        em3 = discord.Embed(title="🏅 Prediction Leaders", color=C_PURPLE)
        top3 = sorted(lb.items(), key=lambda x: x[1].get("points", 0), reverse=True)[:5]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        rows   = []
        for rank, (uid, entry) in enumerate(top3):
            rows.append(f"{medals[rank]} <@{uid}>  **{entry.get('points', 0)} pts**")
        em3.description = "\n".join(rows) if rows else "No predictions yet."
        _footer(em3)
        embeds.append(em3)

    return embeds


# ── Team profile ──────────────────────────────────────────────────────────────

def embed_team(team: dict, recent: list[dict], upcoming: list[dict]) -> discord.Embed:
    name   = team_display(team)
    flag   = team_flag(team)
    crest  = team.get("crest")
    venue  = team.get("venue") or ""
    coach  = (team.get("coach") or {}).get("name") or ""

    em = discord.Embed(
        title=f"{flag}  {name}",
        description=f"**FIFA World Cup 2026**",
        color=C_BLUE,
    )
    if crest:
        em.set_thumbnail(url=crest)
    if coach:
        em.add_field(name="👔 Head Coach", value=coach, inline=True)
    if venue:
        em.add_field(name="🏟️ Stadium",   value=venue, inline=True)

    if recent:
        lines = []
        for m in recent[-5:]:
            opp    = m.get("awayTeam") if m.get("homeTeam", {}).get("id") == team["id"] else m.get("homeTeam")
            opp_n  = team_display(opp or {})
            opp_f  = team_flag(opp or {})
            h, a   = get_score(m, "fullTime")
            status = "W" if (h > a and m.get("homeTeam", {}).get("id") == team["id"]) or \
                           (a > h and m.get("awayTeam", {}).get("id") == team["id"]) else \
                     "L" if h != a else "D"
            badge  = {"W": "✅", "D": "🟡", "L": "❌"}.get(status, "")
            lines.append(f"{badge} vs {opp_f} {opp_n}  **{h}–{a}**")
        em.add_field(name="📊 Recent WC Matches", value="\n".join(lines), inline=False)

    if upcoming:
        lines = []
        for m in upcoming[:3]:
            opp  = m.get("awayTeam") if m.get("homeTeam", {}).get("id") == team["id"] else m.get("homeTeam")
            opp_n = team_display(opp or {})
            opp_f = team_flag(opp or {})
            dt    = parse_dt(m.get("utcDate", ""))
            lines.append(f"🗓️ vs {opp_f} {opp_n}  —  {_ts(dt)}")
        em.add_field(name="📅 Upcoming Fixtures", value="\n".join(lines), inline=False)

    return _footer(em)


# ── Full recap ────────────────────────────────────────────────────────────────

def embed_full_recap(match: dict, detail: dict | None, youtube_url: str | None) -> discord.Embed:
    home  = team_display(match.get("homeTeam", {}))
    away  = team_display(match.get("awayTeam", {}))
    hf    = team_flag(match.get("homeTeam", {}))
    af    = team_flag(match.get("awayTeam", {}))
    h, a  = get_score(detail or match, "fullTime")
    stage = _stage_badge(match)

    em = discord.Embed(
        title=f"📺 Match Report  ·  {stage}",
        description=(
            f"## {hf} {home}  **{h} – {a}**  {away} {af}\n"
            "*Full-time report*"
        ),
        color=C_BLUE,
    )

    goals = (detail or {}).get("goals", [])
    if goals:
        home_id = (match.get("homeTeam") or {}).get("id")
        home_g, away_g = [], []
        for g in goals:
            tid    = (g.get("team") or {}).get("id")
            scorer = (g.get("scorer") or {}).get("name", "OG")
            minute = g.get("minute", "?")
            gt     = g.get("type", "")
            tag    = " *(Pen)*" if gt == "PENALTY" else (" *(OG)*" if gt == "OWN" else "")
            entry  = f"⚽ {scorer}{tag}  {minute}'"
            if tid == home_id:
                home_g.append(entry)
            else:
                away_g.append(entry)
        if home_g:
            em.add_field(name=f"{hf} {home}", value="\n".join(home_g), inline=True)
        if away_g:
            em.add_field(name=f"{af} {away}", value="\n".join(away_g), inline=True)

    cards = [b for b in (detail or {}).get("bookings", []) if b.get("card") in ("RED", "YELLOW_RED")]
    if cards:
        card_lines = [
            f"🟥 {(b.get('player') or {}).get('name', '?')}  {b.get('minute', '?')}'"
            for b in cards
        ]
        em.add_field(name="Red Cards", value="\n".join(card_lines), inline=False)

    if youtube_url:
        em.add_field(
            name="📺 Highlights",
            value=f"[Watch on YouTube]({youtube_url})",
            inline=False,
        )

    home_crest = (match.get("homeTeam") or {}).get("crest")
    if home_crest:
        em.set_thumbnail(url=home_crest)

    return _footer(em, "• 1-hour post-match report")


# ── Daily summary ─────────────────────────────────────────────────────────────

def embed_daily_summary(matches: list[dict], date_str: str) -> discord.Embed:
    em = discord.Embed(
        title=f"📅 World Cup Daily Summary — {date_str}",
        color=C_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_thumbnail(url=WC_ICON)

    if not matches:
        em.description = "No World Cup matches were played today."
        return _footer(em)

    for m in matches:
        home   = team_display(m.get("homeTeam", {}))
        away   = team_display(m.get("awayTeam", {}))
        hf     = team_flag(m.get("homeTeam", {}))
        af     = team_flag(m.get("awayTeam", {}))
        status = m.get("status", "")
        h, a   = get_score(m, "fullTime") if status == "FINISHED" else get_current_score(m)
        badge  = "✅ FT" if status == "FINISHED" else "🔴 Live"
        stage  = _stage_badge(m)
        em.add_field(
            name=f"{hf} {home}  {h}–{a}  {away} {af}",
            value=f"{badge}  ·  *{stage}*",
            inline=False,
        )
    return _footer(em, "• End of day summary")


# ── Help ──────────────────────────────────────────────────────────────────────

def embed_help(categories: dict[str, list[dict]], page: int = 0) -> discord.Embed:
    cat_names = list(categories.keys())
    total     = len(cat_names)

    if page >= total:
        page = 0

    cat_name = cat_names[page]
    commands = categories[cat_name]

    CAT_ICONS = {
        "Match Commands": "⚽",
        "Predictions":    "🎯",
        "Following":      "⭐",
        "Settings":       "⚙️",
        "Admin":          "🔧",
    }
    icon = CAT_ICONS.get(cat_name, "📋")

    em = discord.Embed(
        title=f"{icon} Help — {cat_name}",
        description=f"Page {page + 1}/{total}  ·  Use the buttons to navigate categories.",
        color=C_GOLD,
    )
    em.set_thumbnail(url=WC_ICON)

    for cmd in commands:
        em.add_field(
            name=f"/{cmd['name']}",
            value=cmd.get("description", "No description"),
            inline=False,
        )

    return _footer(em, f"• {total} categories")


# ── Dashboard panel ───────────────────────────────────────────────────────────

def embed_dashboard() -> discord.Embed:
    em = discord.Embed(
        title="⚽ FIFA World Cup 2026 — Dashboard",
        description=(
            "Your all-in-one World Cup companion.\n"
            "Click a button to get live data instantly."
        ),
        color=C_GOLD,
    )
    em.set_thumbnail(url=WC_ICON)
    em.add_field(name="⚽ Live Matches",  value="Currently live games",           inline=True)
    em.add_field(name="📅 Schedule",      value="Today's & upcoming fixtures",    inline=True)
    em.add_field(name="🏆 Standings",     value="Group tables",                   inline=True)
    em.add_field(name="🎯 Predictions",   value="Score predictions & stats",      inline=True)
    em.add_field(name="⭐ Favorites",     value="Your followed national teams",   inline=True)
    em.add_field(name="🔔 Notifications", value="Personal notification settings", inline=True)
    em.add_field(name="⚙️ Settings",      value="Server mode & timezone",         inline=True)
    return _footer(em, "• All responses are private to you")


# ── Test embed (admin /testembed command) ─────────────────────────────────────

_TEST_MATCH: dict = {
    "id": 99999,
    "utcDate": "2026-06-15T18:00:00Z",
    "status": "IN_PLAY",
    "minute": "75",
    "stage": "GROUP_STAGE",
    "group": "GROUP_A",
    "homeTeam": {"id": 1, "name": "Brazil",    "shortName": "Brazil",    "tla": "BRA", "crest": ""},
    "awayTeam": {"id": 2, "name": "Argentina", "shortName": "Argentina", "tla": "ARG", "crest": ""},
    "score": {
        "fullTime":    {"home": 2, "away": 1},
        "halfTime":    {"home": 1, "away": 0},
        "regularTime": None,
        "extraTime":   None,
        "penalties":   None,
        "winner":      "HOME_TEAM",
        "duration":    "REGULAR",
    },
    "competition": {"name": "FIFA World Cup", "code": "WC"},
    "venue": "Estadio Azteca, Mexico City",
}

_TEST_DETAIL: dict = {
    **_TEST_MATCH,
    "goals": [
        {"minute": "23", "type": "REGULAR", "scorer": {"name": "Vinícius Jr."},     "assist": {"name": "Rodrygo"},        "team": {"name": "Brazil"}},
        {"minute": "41", "type": "REGULAR", "scorer": {"name": "Lautaro Martínez"}, "assist": None,                       "team": {"name": "Argentina"}},
        {"minute": "67", "type": "REGULAR", "scorer": {"name": "Raphinha"},          "assist": {"name": "Vinícius Jr."}, "team": {"name": "Brazil"}},
    ],
    "bookings": [
        {"minute": "55", "type": "RED_CARD", "player": {"name": "Nicolás Otamendi"}, "team": {"name": "Argentina"}},
    ],
    "lineups": [
        {
            "team": {"name": "Brazil"},
            "formation": "4-3-3",
            "startXI": [{"player": {"name": n}} for n in [
                "Ederson", "Danilo", "Marquinhos", "Éder Militão", "Guilherme Arana",
                "Casemiro", "Bruno Guimarães", "Lucas Paquetá",
                "Rodrygo", "Vinícius Jr.", "Raphinha",
            ]],
            "bench": [{"player": {"name": n}} for n in ["Weverton", "Gabriel Magalhães", "Endrick"]],
        },
        {
            "team": {"name": "Argentina"},
            "formation": "4-3-3",
            "startXI": [{"player": {"name": n}} for n in [
                "Emiliano Martínez", "Nahuel Molina", "Cristian Romero", "Nicolás Otamendi", "Nicolás Tagliafico",
                "Rodrigo De Paul", "Leandro Paredes", "Alexis Mac Allister",
                "Ángel Di María", "Lionel Messi", "Lautaro Martínez",
            ]],
            "bench": [{"player": {"name": n}} for n in ["Franco Armani", "Germán Pezzella", "Alejandro Garnacho"]],
        },
    ],
    "referees": [{"name": "Pierluigi Collina", "role": "REFEREE"}],
}

_TEST_GOAL: dict = {
    "minute": "67", "type": "REGULAR",
    "scorer": {"name": "Raphinha"},
    "assist": {"name": "Vinícius Jr."},
    "team": {"name": "Brazil"},
    "score": {"home": 2, "away": 1},
}

_TEST_CARD: dict = {
    "minute": "55", "type": "RED_CARD",
    "player": {"name": "Nicolás Otamendi"},
    "team": {"name": "Argentina"},
}

_TEST_MATCH_ET: dict = {
    **_TEST_MATCH,
    "status": "EXTRA_TIME",
    "minute": "102",
    "score": {**_TEST_MATCH["score"], "fullTime": {"home": 1, "away": 1}},
}

_TEST_MATCH_PSO: dict = {
    **_TEST_MATCH,
    "status": "PENALTY_SHOOTOUT",
    "stage": "ROUND_OF_16",
    "group": None,
    "score": {**_TEST_MATCH["score"], "fullTime": {"home": 1, "away": 1}},
}

_TEST_MATCHES_TODAY: list = [_TEST_MATCH, {
    **_TEST_MATCH,
    "id": 99998,
    "homeTeam": {"id": 3, "name": "France",  "shortName": "France",  "tla": "FRA", "crest": ""},
    "awayTeam": {"id": 4, "name": "England", "shortName": "England", "tla": "ENG", "crest": ""},
    "status": "SCHEDULED",
    "utcDate": "2026-06-15T21:00:00Z",
    "score": {
        "fullTime": {"home": None, "away": None}, "halfTime": {"home": None, "away": None},
        "regularTime": None, "extraTime": None, "penalties": None,
        "winner": None, "duration": "REGULAR",
    },
}]


def embed_test(embed_type: str) -> discord.Embed:
    """Return a realistic notification embed for admin testing via /testembed."""
    m  = _TEST_MATCH
    d  = _TEST_DETAIL

    if embed_type == "reminder15":
        return embed_reminder(m, 15)
    if embed_type == "kickoff":
        return embed_kickoff(m, d)
    if embed_type == "lineup":
        return embed_lineups(m, d)
    if embed_type == "goal":
        return embed_goal(m, d, _TEST_GOAL)
    if embed_type == "redcard":
        return embed_red_card(m, d, _TEST_CARD)
    if embed_type == "halftime":
        return embed_halftime(m, d)
    if embed_type == "secondhalf":
        return embed_second_half(m)
    if embed_type == "extratime":
        return embed_extra_time(_TEST_MATCH_ET, d)
    if embed_type == "pso":
        return embed_penalty_shootout(_TEST_MATCH_PSO, d)
    if embed_type == "fulltime":
        return embed_fulltime(m, d)
    if embed_type == "motm_vote":
        return embed_motm_vote(m)
    if embed_type == "motm_result":
        tally = {"Vinícius Jr.": 14, "Raphinha": 7, "Lautaro Martínez": 3}
        return embed_motm_result(m, ["Vinícius Jr."], tally)
    if embed_type == "prediction":
        return embed_prediction_poll(m)
    if embed_type == "predresults":
        exact   = [("111111111", 2, 1), ("222222222", 2, 1)]
        correct = [("333333333", 1, 0), ("444444444", 3, 2)]
        return embed_prediction_results(m, 2, 1, exact, correct)
    if embed_type == "recap":
        return embed_full_recap(m, d, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    if embed_type == "daily":
        return embed_daily_summary(_TEST_MATCHES_TODAY, "15 June 2026")

    return discord.Embed(description=f"⚠️ Unknown test type: `{embed_type}`", color=C_RED)


# ── Competitions list ──────────────────────────────────────────────────────────

def embed_competitions() -> discord.Embed:
    em = discord.Embed(
        title="🏆 Supported Competitions",
        description=(
            "This bot tracks **FIFA World Cup 2026** exclusively.\n\n"
            "| Code | Competition |\n"
            "|------|-------------|\n"
            "| `WC` | FIFA World Cup 2026 |"
        ),
        color=C_GOLD,
    )
    em.add_field(
        name="ℹ️ Usage",
        value=(
            "Use `WC` as the competition code in any command that accepts one.\n"
            "Follow the tournament with `/followcompetition WC`."
        ),
        inline=False,
    )
    return _footer(em)


# ── Status ────────────────────────────────────────────────────────────────────

def embed_status(guild: Any, cfg: dict, monitor_running: bool) -> discord.Embed:
    em = discord.Embed(title="⚙️ Bot Configuration", color=C_BLUE)
    if guild:
        em.set_author(name=guild.name, icon_url=guild.icon and str(guild.icon))

    def _ch(key: str) -> str:
        cid = cfg.get(key)
        if not cid:
            return "❌ Not set"
        ch = guild.get_channel(int(cid)) if guild else None
        return ch.mention if ch else f"#{cid}"

    mode   = cfg.get("mode", "standard")
    tz_str = cfg.get("timezone", "UTC")

    mode_icons = {"quiet": "🔇", "standard": "📢", "detailed": "📋"}
    mode_label = f"{mode_icons.get(mode, '📢')} {mode.capitalize()}"

    mon = "✅ Active (every 2 min)" if monitor_running else "❌ Stopped"
    if not cfg.get("channel_id"):
        mon = "⚠️ Disabled — run `/setchannel`"

    em.add_field(name="📡 Live Alerts",   value=_ch("channel_id"),             inline=True)
    em.add_field(name="🎯 Predictions",   value=_ch("predictions_channel_id"), inline=True)
    em.add_field(name="🏆 Results",       value=_ch("results_channel_id"),     inline=True)
    em.add_field(name="📊 Recaps",        value=_ch("summary_channel_id"),     inline=True)
    em.add_field(name="🎛️ Commands",      value=_ch("commands_channel_id"),    inline=True)
    em.add_field(name="\u200b",           value="\u200b",                      inline=True)
    em.add_field(name="🔔 Mode",          value=mode_label,                    inline=True)
    em.add_field(name="🕐 Timezone",      value=f"`{tz_str}`",                 inline=True)
    em.add_field(name="📡 Monitoring",    value=mon,                           inline=True)

    return _footer(em)


# ── Commands menu ─────────────────────────────────────────────────────────────

def embed_commands_menu() -> discord.Embed:
    em = discord.Embed(
        title="🎛️ Notification Mode",
        description=(
            "Choose how many notifications you receive during World Cup matches.\n\n"
            "🔇 **Quiet** — Goals + Red Cards + Full Time only\n"
            "📢 **Standard** — + Half Time, Extra Time, MOTM Polls, Predictions\n"
            "📋 **Detailed** — Everything: Lineups, Recaps, Group Tables, Second Half"
        ),
        color=C_BLUE,
    )
    em.set_footer(text="This only affects your personal notifications, not the whole server.")
    return em


# ── Setup wizard ──────────────────────────────────────────────────────────────

def embed_setup_intro() -> discord.Embed:
    em = discord.Embed(
        title="⚙️ World Cup Bot Setup Wizard",
        description=(
            "Welcome! Let's configure your server in a few easy steps.\n\n"
            "**What we'll set up:**\n"
            "1️⃣ Live match alerts channel\n"
            "2️⃣ Predictions & MOTM channel\n"
            "3️⃣ Results channel\n"
            "4️⃣ Match recaps channel\n"
            "5️⃣ Timezone\n"
            "6️⃣ Notification mode\n\n"
            "Click **Start Setup** to begin — all steps are optional."
        ),
        color=C_GOLD,
    )
    em.set_thumbnail(url=WC_ICON)
    return _footer(em)


def embed_setup_complete(cfg: dict, guild: Any) -> discord.Embed:
    em = discord.Embed(
        title="✅ Setup Complete!",
        description="Your World Cup 2026 bot is ready. Here's your configuration:",
        color=C_GREEN,
    )

    def _ch(key: str) -> str:
        cid = cfg.get(key)
        if not cid:
            return "Not set"
        ch = guild.get_channel(int(cid)) if guild else None
        return ch.mention if ch else f"#{cid}"

    em.add_field(name="📡 Live Alerts",   value=_ch("channel_id"),             inline=True)
    em.add_field(name="🎯 Predictions",   value=_ch("predictions_channel_id"), inline=True)
    em.add_field(name="🏆 Results",       value=_ch("results_channel_id"),     inline=True)
    em.add_field(name="📊 Recaps",        value=_ch("summary_channel_id"),     inline=True)
    em.add_field(name="🔔 Mode",          value=cfg.get("mode", "standard"),   inline=True)
    em.add_field(name="🕐 Timezone",      value=cfg.get("timezone", "UTC"),    inline=True)
    em.add_field(
        name="Next Steps",
        value=(
            "• `/setinteractivechannel` — post the user panel\n"
            "• `/dashboard` — post the interactive dashboard\n"
            "• `/help` — see all available commands"
        ),
        inline=False,
    )
    return _footer(em)
