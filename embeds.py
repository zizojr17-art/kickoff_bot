"""
All Discord embed builders — one function per notification type.

Design rules:
- WC matches: show Stage · Match N instead of "FIFA World Cup 2026".
- All kick-off times use Discord <t:…> timestamps (auto local-timezone).
"""
import discord
from datetime import datetime, timezone
from typing import Optional

from football_api import (
    team_display,
    get_score,
    get_current_score,
    get_stage,
    get_venue,
    penalty_scores,
    parse_dt,
    flag_emoji,
    wc_match_number,
    COMPETITION_CODES,
    STAGE_NAMES,
)

# ── Colours ───────────────────────────────────────────────────────────────────
C_BLUE       = 0x3498DB
C_GREEN      = 0x2ECC71
C_GOLD       = 0xF1C40F
C_RED        = 0xE74C3C
C_ORANGE     = 0xE67E22
C_PURPLE     = 0x9B59B6
C_DARK_BLUE  = 0x1A1A2E
C_GREY       = 0x95A5A6
C_WHITE      = 0xECF0F1


# ── Shared helpers ────────────────────────────────────────────────────────────

def _footer(embed: discord.Embed, text: str = "football-data.org • Kickoff Bot") -> discord.Embed:
    embed.set_footer(text=text)
    return embed


def _ts(dt: datetime, style: str = "f") -> str:
    """Discord timestamp — renders in the viewer's local timezone automatically."""
    return f"<t:{int(dt.timestamp())}:{style}>"


def _match_header(match: dict) -> tuple[str, str, str]:
    home = team_display(match.get("homeTeam", {}))
    away = team_display(match.get("awayTeam", {}))
    comp = match.get("competition", {}).get("name", "Football")
    return home, away, comp


def _comp_icon(match: dict) -> Optional[str]:
    return match.get("competition", {}).get("emblem") or None


def _is_wc(match: dict) -> bool:
    return match.get("competition", {}).get("code") == "WC"


def _stage_label(match: dict) -> str:
    """'Group A', 'Semi-Finals', 'Final', etc."""
    raw_group = match.get("group") or ""
    if raw_group.startswith("GROUP_"):
        return f"Group {raw_group.replace('GROUP_', '')}"
    return get_stage(match)


def _match_label(match: dict) -> str:
    """
    WC  → 'Group A  ·  Match 5'  (no competition name)
    Other → 'UEFA Champions League  ·  Quarter-finals'
    """
    if _is_wc(match):
        stage = _stage_label(match)
        num = wc_match_number(match.get("id"))
        return f"{stage}  ·  Match {num}" if num else stage
    comp  = match.get("competition", {}).get("name", "Football")
    return f"{comp}  ·  {_stage_label(match)}"


def _home_name(match: dict) -> str:
    return match.get("homeTeam", {}).get("shortName") or match.get("homeTeam", {}).get("name", "Home")


def _away_name(match: dict) -> str:
    return match.get("awayTeam", {}).get("shortName") or match.get("awayTeam", {}).get("name", "Away")


# ── Score prediction prompt ───────────────────────────────────────────────────

def embed_prediction_prompt(match: dict) -> discord.Embed:
    """Embed sent to predictions_channel. Has a 🎯 Predict Score button attached in bot.py."""
    home, away, _ = _match_header(match)
    ko_dt = parse_dt(match.get("utcDate"))
    label = _match_label(match)

    em = discord.Embed(
        title="🗳️  Score Prediction",
        description=(
            f"**{home}  vs  {away}**\n"
            f"{label}"
            + (f"\nKick-off {_ts(ko_dt, 'R')}" if ko_dt else "")
            + "\n\nTap **🎯 Predict Score** to enter your exact score prediction."
        ),
        color=C_GOLD,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Pre-match reminders ───────────────────────────────────────────────────────

def embed_reminder(match: dict, mins_before: int) -> discord.Embed:
    home, away, _ = _match_header(match)
    ko_dt = parse_dt(match.get("utcDate"))
    venue = get_venue(match)
    label = _match_label(match)

    title = "⏰  1 Hour to Kick-off" if mins_before >= 60 else "🔔  15 Minutes to Kick-off"
    color = C_BLUE if mins_before >= 60 else C_ORANGE

    em = discord.Embed(
        title=title,
        description=f"**{home}  vs  {away}**\n{label}",
        color=color,
    )
    if ko_dt:
        em.add_field(name="Kick-off",   value=_ts(ko_dt, "R"), inline=True)
        em.add_field(name="Local time", value=_ts(ko_dt, "t"), inline=True)
    if venue:
        em.add_field(name="Venue", value=venue, inline=False)
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Lineups ───────────────────────────────────────────────────────────────────

def embed_lineup(match: dict, detail: dict) -> discord.Embed:
    home, away, _ = _match_header(match)
    label = _match_label(match)

    home_team = detail.get("homeTeam") or {}
    away_team = detail.get("awayTeam") or {}
    home_lu   = (home_team.get("lineup") or [])[:11]
    away_lu   = (away_team.get("lineup") or [])[:11]
    home_form = home_team.get("formation") or "?"
    away_form = away_team.get("formation") or "?"

    em = discord.Embed(
        title=f"📋  Starting Lineups  —  {home}  vs  {away}",
        description=label,
        color=C_BLUE,
    )

    def _fmt_player(p: dict) -> str:
        num  = p.get("shirtNumber") or "?"
        name = p.get("name") or "Unknown"
        return f"`{str(num).rjust(2)}`  {name}"

    if home_lu:
        em.add_field(
            name=f"{home}  ({home_form})",
            value="\n".join(_fmt_player(p) for p in home_lu) or "—",
            inline=True,
        )
    if away_lu:
        em.add_field(
            name=f"{away}  ({away_form})",
            value="\n".join(_fmt_player(p) for p in away_lu) or "—",
            inline=True,
        )

    return _footer(em, "Lineups officially confirmed · football-data.org")


# ── Kick-off ──────────────────────────────────────────────────────────────────

def embed_kickoff(match: dict) -> discord.Embed:
    home, away, _ = _match_header(match)
    venue = get_venue(match)
    label = _match_label(match)

    em = discord.Embed(
        title="🚀  KICK-OFF!",
        description=f"**{home}  vs  {away}**\n{label}",
        color=C_GREEN,
    )
    if venue:
        em.add_field(name="Venue", value=venue, inline=True)
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Goal ─────────────────────────────────────────────────────────────────────

def embed_goal(match: dict, detail: Optional[dict], goal: Optional[dict] = None) -> discord.Embed:
    home, away, _ = _match_header(match)
    h, a = get_current_score(detail or match)
    score_str = f"{h}–{a}" if h is not None else "? – ?"
    label = _match_label(match)

    scorer_line = ""
    if goal:
        scorer = (goal.get("scorer") or {}).get("name", "")
        minute = goal.get("minute")
        g_type = goal.get("type", "")
        prefix = "🥅 OG" if g_type == "OWN" else ("⚽ Pen" if g_type == "PENALTY" else "⚽")
        minute_str = f" ({minute}')" if minute else ""
        if scorer:
            scorer_line = f"{prefix}  **{scorer}**{minute_str}"

    scoring_team_id = (goal.get("team") or {}).get("id") if goal else None
    home_id = match.get("homeTeam", {}).get("id")
    scoring_team = home if scoring_team_id == home_id else away

    em = discord.Embed(
        title="⚽  GOAL!",
        description=f"**{home}  {score_str}  {away}**\n{label}",
        color=C_GOLD,
    )
    if scorer_line:
        em.add_field(name=scoring_team, value=scorer_line, inline=False)
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Red card ──────────────────────────────────────────────────────────────────

def embed_red_card(match: dict, detail: Optional[dict], booking: Optional[dict] = None) -> discord.Embed:
    home, away, _ = _match_header(match)
    h, a = get_current_score(detail or match)
    score_str = f"{h}–{a}" if h is not None else ""
    label = _match_label(match)

    card_type  = (booking or {}).get("card", "RED")
    card_label = "🟥  RED CARD" if card_type == "RED" else "🟨🟥  SECOND YELLOW"

    player     = (booking or {}).get("player", {}) or {}
    player_name = player.get("name", "")
    minute     = (booking or {}).get("minute")
    team_id    = ((booking or {}).get("team") or {}).get("id")
    home_id    = match.get("homeTeam", {}).get("id")
    team_name  = home if team_id == home_id else away

    desc = f"**{home}  {score_str}  {away}**\n{label}" if score_str else f"**{home}  vs  {away}**\n{label}"

    em = discord.Embed(title=card_label, description=desc, color=C_RED)
    if player_name:
        minute_str = f" ({minute}')" if minute else ""
        em.add_field(name=team_name, value=f"**{player_name}**{minute_str}", inline=False)
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Half-time ─────────────────────────────────────────────────────────────────

def embed_halftime(match: dict, detail: Optional[dict] = None) -> discord.Embed:
    home, away, _ = _match_header(match)
    d = detail or match
    h, a = get_score(d, "halfTime")
    if h is None:
        h, a = get_current_score(d)
    score_str = f"{h}–{a}" if h is not None else "? – ?"
    label = _match_label(match)

    em = discord.Embed(
        title="⏸  HALF-TIME",
        description=f"**{home}  {score_str}  {away}**\n{label}",
        color=C_ORANGE,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Second half ───────────────────────────────────────────────────────────────

def embed_second_half(match: dict) -> discord.Embed:
    home, away, _ = _match_header(match)
    em = discord.Embed(
        title="▶️  SECOND HALF UNDERWAY",
        description=f"**{home}  vs  {away}**\n{_match_label(match)}",
        color=C_BLUE,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Extra time ────────────────────────────────────────────────────────────────

def embed_extra_time(match: dict, detail: Optional[dict] = None) -> discord.Embed:
    home, away, _ = _match_header(match)
    h, a = get_current_score(detail or match)
    score_str = f"{h}–{a}" if h is not None else "? – ?"

    em = discord.Embed(
        title="⏱️  EXTRA TIME",
        description=f"**{home}  {score_str}  {away}**\n{_match_label(match)}",
        color=C_PURPLE,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Penalty shootout ──────────────────────────────────────────────────────────

def embed_penalty_shootout(match: dict, detail: Optional[dict] = None) -> discord.Embed:
    home, away, _ = _match_header(match)
    em = discord.Embed(
        title="🎯  PENALTY SHOOTOUT!",
        description=f"**{home}  vs  {away}**\n{_match_label(match)}",
        color=C_PURPLE,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Full-time ─────────────────────────────────────────────────────────────────

def embed_fulltime(match: dict, detail: Optional[dict] = None) -> discord.Embed:
    """
    Title:  🏁 Full Time — Home H-A Away
    Fields: ⚽ Goals | 🟥 Red Cards (if any) | 🏟️ Match
    Footer: Full stats + highlights posting in 1 hour
    """
    home, away, comp = _match_header(match)
    d = detail or match
    h, a = get_score(d, "fullTime")
    if h is None:
        h, a = get_current_score(d)
    score_str = f"{h}–{a}" if h is not None else "?"

    # Penalty result
    pen_h, pen_a = penalty_scores(d)
    pen_line = ""
    if pen_h is not None and pen_a is not None:
        pen_winner = _home_name(match) if pen_h > pen_a else _away_name(match)
        pen_line = f"\nPenalties: **{pen_h}–{pen_a}**  ·  {pen_winner} advance"

    em = discord.Embed(
        title=f"🏁  Full Time  —  {_home_name(match)} {score_str} {_away_name(match)}",
        description=f"**{home}  {score_str}  {away}**{pen_line}",
        color=C_GREEN,
    )

    # ⚽ Goals
    goals = (d.get("goals") or []) if d else []
    if goals:
        lines = []
        for g in goals:
            minute  = g.get("minute", "?")
            scorer  = (g.get("scorer") or {}).get("name", "Unknown")
            assist  = (g.get("assist") or {}).get("name")
            team_id = (g.get("team") or {}).get("id")
            home_id = match.get("homeTeam", {}).get("id")
            side    = _home_name(match) if team_id == home_id else _away_name(match)
            g_type  = g.get("type", "")
            prefix  = "🥅" if g_type == "OWN" else ("⚽P" if g_type == "PENALTY" else "⚽")
            line    = f"{prefix} **{scorer}** ({minute}')  · {side}"
            if assist:
                line += f"  ·  assist: {assist}"
            lines.append(line)
        em.add_field(name="⚽  Goals", value="\n".join(lines), inline=False)

    # 🟥 Red cards
    bookings = (d.get("bookings") or []) if d else []
    red_cards = [b for b in bookings if b.get("card") in ("RED", "YELLOW_RED")]
    if red_cards:
        lines = []
        for b in red_cards:
            minute  = b.get("minute", "?")
            player  = (b.get("player") or {}).get("name", "Unknown")
            team_id = (b.get("team") or {}).get("id")
            home_id = match.get("homeTeam", {}).get("id")
            side    = _home_name(match) if team_id == home_id else _away_name(match)
            card    = "🟥" if b.get("card") == "RED" else "🟨🟥"
            lines.append(f"{card} **{player}** ({minute}')  · {side}")
        em.add_field(name="🟥  Red Cards", value="\n".join(lines), inline=False)

    # 🏟️ Match info
    venue = get_venue(match)
    match_info_parts = [_match_label(match)]
    if venue:
        match_info_parts.append(f"🏟️ {venue}")
    em.add_field(name="🏟️  Match", value="\n".join(match_info_parts), inline=False)

    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)

    return _footer(em, "Full stats + highlights posting in 1 hour")


# ── MOTM vote ─────────────────────────────────────────────────────────────────

def embed_motm_vote(match: dict, nominees: list[str]) -> discord.Embed:
    home, away, _ = _match_header(match)
    label = _match_label(match)

    em = discord.Embed(
        title="🌟  Man of the Match — Vote Now!",
        description=(
            f"**{home}  vs  {away}**\n"
            f"{label}\n\n"
            "Who has been the best player so far? Voting closes at full time."
        ),
        color=C_GOLD,
    )
    em.add_field(
        name="Nominees",
        value="\n".join(f"• {n}" for n in nominees) or "—",
        inline=False,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── MOTM result ───────────────────────────────────────────────────────────────

def embed_motm_result(match: dict, winners: list[str], tally: dict[str, int]) -> discord.Embed:
    home, away, _ = _match_header(match)

    if len(winners) == 1:
        title  = f"🌟  Man of the Match: {winners[0]}"
        desc   = f"**{home}  vs  {away}**\n{_match_label(match)}\n\nCongratulations to all correct voters — **+1 point** awarded!"
    else:
        title  = f"🌟  Man of the Match: Tie — {', '.join(winners)}"
        desc   = f"**{home}  vs  {away}**\n{_match_label(match)}\n\nTie! All voters for either winner receive **+1 point**."

    em = discord.Embed(title=title, description=desc, color=C_GOLD)

    # Show vote breakdown (top 5 by votes)
    sorted_tally = sorted(tally.items(), key=lambda x: x[1], reverse=True)[:5]
    if sorted_tally:
        lines = [f"{'🥇' if p == winners[0] else '·'}  **{p}** — {v} vote{'s' if v != 1 else ''}"
                 for p, v in sorted_tally]
        em.add_field(name="Vote breakdown", value="\n".join(lines), inline=False)

    return _footer(em)


# ── Prediction results ────────────────────────────────────────────────────────

def embed_prediction_results(
    match: dict,
    final_home: int,
    final_away: int,
    exact_winners: list[tuple],    # [(user_id, pred_home, pred_away), …]
    result_winners: list[tuple],   # [(user_id, pred_home, pred_away), …]
) -> discord.Embed:
    home, away, _ = _match_header(match)
    score_str = f"{final_home}–{final_away}"
    label = _match_label(match)

    em = discord.Embed(
        title=f"🏆  Prediction Results  —  {_home_name(match)} {score_str} {_away_name(match)}",
        description=f"**{home}  {score_str}  {away}**\n{label}",
        color=C_GOLD,
    )

    if exact_winners:
        lines = [f"🎯 <@{uid}> predicted **{ph}–{pa}** → **+3 pts**"
                 for uid, ph, pa in exact_winners[:10]]
        em.add_field(name=f"✅  Exact score ({len(exact_winners)} winner{'s' if len(exact_winners)!=1 else ''})",
                     value="\n".join(lines), inline=False)
    else:
        em.add_field(name="Exact score", value="No one predicted the exact score.", inline=False)

    if result_winners:
        lines = [f"· <@{uid}> predicted **{ph}–{pa}** → **+1 pt**"
                 for uid, ph, pa in result_winners[:10]]
        em.add_field(name=f"✔️  Correct result ({len(result_winners)} winner{'s' if len(result_winners)!=1 else ''})",
                     value="\n".join(lines), inline=False)

    return _footer(em)


# ── Leaderboard ───────────────────────────────────────────────────────────────

def embed_leaderboard(guild: discord.Guild, lb: dict[str, int]) -> discord.Embed:
    em = discord.Embed(
        title="🏆  Prediction Leaderboard",
        color=C_GOLD,
        timestamp=datetime.now(timezone.utc),
    )

    if not lb:
        em.description = "No points on the board yet. Start predicting!"
        return _footer(em)

    sorted_lb = sorted(lb.items(), key=lambda x: x[1], reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, (uid, pts) in enumerate(sorted_lb[:20]):
        medal  = medals[i] if i < 3 else f"`{i+1}.`"
        member = guild.get_member(int(uid))
        name   = member.display_name if member else f"User {uid}"
        lines.append(f"{medal}  **{name}** — {pts} pt{'s' if pts != 1 else ''}")

    em.description = "\n".join(lines)
    return _footer(em)


# ── 1-hour recap ──────────────────────────────────────────────────────────────

def embed_full_recap(match: dict, detail: Optional[dict], youtube: Optional[dict]) -> discord.Embed:
    home, away, _ = _match_header(match)
    d = detail or match
    h, a = get_score(d, "fullTime")
    score_str = f"{h}–{a}" if h is not None else "?"
    label = _match_label(match)

    em = discord.Embed(
        title=f"📊  Match Recap  —  {_home_name(match)} {score_str} {_away_name(match)}",
        description=f"**{home}  {score_str}  {away}**\n{label}",
        color=C_DARK_BLUE,
    )

    # 📈 Match Stats
    stats = (d.get("statistics") or []) if d else []
    if stats:
        def _find(label_: str):
            for s in stats:
                if label_.lower() in (s.get("type") or "").lower():
                    return str(s.get("home") or ""), str(s.get("away") or "")
            return "", ""

        pos_h,   pos_a   = _find("possession")
        shots_h, shots_a = _find("total_shots")
        sot_h,   sot_a   = _find("shots_on_target")
        corners_h, corners_a = _find("corner")
        fouls_h, fouls_a = _find("fouls")

        stat_lines = []
        if pos_h or pos_a:
            stat_lines.append(f"Possession       {pos_h} — {pos_a}")
        if shots_h or shots_a:
            stat_lines.append(f"Shots            {shots_h} — {shots_a}")
        if sot_h or sot_a:
            stat_lines.append(f"Shots on target  {sot_h} — {sot_a}")
        if corners_h or corners_a:
            stat_lines.append(f"Corners          {corners_h} — {corners_a}")
        if fouls_h or fouls_a:
            stat_lines.append(f"Fouls            {fouls_h} — {fouls_a}")

        if stat_lines:
            header = f"`{'Stat':<17} {_home_name(match)[:8]:>8} — {_away_name(match)[:8]:<8}`"
            em.add_field(name="📈  Match Stats",
                         value=header + "\n" + "\n".join(f"`{l}`" for l in stat_lines),
                         inline=False)

    # ⚽ Top Performers (goal scorers + assisters)
    goals = (d.get("goals") or []) if d else []
    if goals:
        performers: dict[str, int] = {}
        for g in goals:
            if g.get("type") != "OWN":
                name = (g.get("scorer") or {}).get("name")
                if name:
                    performers[name] = performers.get(name, 0) + 1
            assist_name = (g.get("assist") or {}).get("name")
            if assist_name:
                performers[assist_name] = performers.get(assist_name, 0) + 1

        if performers:
            top = sorted(performers.items(), key=lambda x: x[1], reverse=True)[:6]
            lines = [f"⚽ **{name}** × {n}" for name, n in top]
            em.add_field(name="⚽  Top Performers", value="\n".join(lines), inline=False)

    # 🎬 Highlights
    if youtube:
        em.add_field(
            name="🎬  Highlights",
            value=(
                f"[{youtube['title']}]({youtube['url']})\n"
                f"📺 {youtube.get('channel', '')}"
            ),
            inline=False,
        )
        if youtube.get("thumbnail"):
            em.set_image(url=youtube["thumbnail"])
    else:
        em.add_field(
            name="🎬  Highlights",
            value="Highlights not found yet — search YouTube for the match name.",
            inline=False,
        )

    return _footer(em)


# ── Commands menu ─────────────────────────────────────────────────────────────

def embed_commands_menu() -> discord.Embed:
    em = discord.Embed(
        title="⚽  Kickoff Bot — Command Centre",
        description=(
            "Use the buttons below to set your personal notification preference.\n"
            "All slash commands are also available — type `/` to see them."
        ),
        color=C_DARK_BLUE,
    )
    em.add_field(
        name="🔇  Quiet",
        value="Goals + Full Time only",
        inline=True,
    )
    em.add_field(
        name="📢  Standard",
        value="Goals, Red Cards, Half-time + Full Time",
        inline=True,
    )
    em.add_field(
        name="📋  Detailed",
        value="Everything: lineups, all events, stats, recaps",
        inline=True,
    )
    em.add_field(
        name="📌  Quick Setup",
        value=(
            "`/setchannel` — live match events\n"
            "`/setpredictionschannel` — prediction polls + MOTM\n"
            "`/setresultschannel` — results, points & leaderboard\n"
            "`/setsummarychannel` — recaps & highlights\n"
            "`/setcommandschannel` — this menu"
        ),
        inline=False,
    )
    return _footer(em)


# ── WC group update (pinnable) ────────────────────────────────────────────────

def embed_wc_group_update(group_letter: str, table: list[dict]) -> discord.Embed:
    return embed_wc_group(group_letter, table)


# ── Today's matches ────────────────────────────────────────────────────────────

def embed_today(matches: list[dict]) -> list[discord.Embed]:
    if not matches:
        em = discord.Embed(title="📅  Today's Matches", description="No matches today.", color=C_GREY)
        return [_footer(em)]

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%d %b %Y")

    by_comp: dict[str, list] = {}
    for m in matches:
        comp = m.get("competition", {}).get("name", "Other")
        by_comp.setdefault(comp, []).append(m)

    embeds = []
    em = discord.Embed(title=f"📅  Today's Matches  —  {today_str}", color=C_BLUE)

    for comp, ms in by_comp.items():
        lines = []
        for m in ms:
            home, away, _ = _match_header(m)
            status = m.get("status", "")
            ko_dt  = parse_dt(m.get("utcDate"))

            if status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
                h, a = get_current_score(m)
                score = f"{h}–{a}" if h is not None else "?"
                st = {"IN_PLAY": "🔴 LIVE", "PAUSED": "⏸ HT", "EXTRA_TIME": "⏱ ET",
                      "PENALTY_SHOOTOUT": "🎯 PSO"}.get(status, status)
                lines.append(f"{st}  **{home} {score} {away}**")
            elif status == "FINISHED":
                h, a = get_score(m, "fullTime")
                score = f"{h}–{a}" if h is not None else "FT"
                lines.append(f"✅  **{home} {score} {away}**")
            else:
                ts = _ts(ko_dt, "t") if ko_dt else "TBD"
                lines.append(f"🕐 {ts}  {home} vs {away}")

        if lines:
            chunk, chunks = "", []
            for line in lines:
                if len(chunk) + len(line) + 1 > 1000:
                    chunks.append(chunk.rstrip())
                    chunk = line + "\n"
                else:
                    chunk += line + "\n"
            if chunk.strip():
                chunks.append(chunk.rstrip())
            for i, c in enumerate(chunks):
                if len(em.fields) >= 25:
                    embeds.append(_footer(em))
                    em = discord.Embed(color=C_BLUE)
                em.add_field(name=comp if i == 0 else f"{comp} (cont.)", value=c, inline=False)

    embeds.append(_footer(em))
    return embeds


# ── Live matches ───────────────────────────────────────────────────────────────

def embed_live(matches: list[dict]) -> discord.Embed:
    if not matches:
        em = discord.Embed(title="⚽  Live Matches", description="No matches live right now.", color=C_GREY)
        return _footer(em)

    em = discord.Embed(title="⚽  Live Matches", color=C_RED)
    for m in matches:
        home, away, _ = _match_header(m)
        h, a = get_current_score(m)
        score = f"{h}–{a}" if h is not None else "? – ?"
        status = m.get("status", "")
        st = {"PAUSED": "⏸ HT", "EXTRA_TIME": "⏱ ET", "PENALTY_SHOOTOUT": "🎯 PSO"}.get(status, "🔴 LIVE")
        em.add_field(
            name=f"{st}  {home}  {score}  {away}",
            value=_match_label(m),
            inline=False,
        )
    return _footer(em)


# ── Standings ──────────────────────────────────────────────────────────────────

def embed_standings(data: dict) -> list[discord.Embed]:
    comp_name = data.get("competition", {}).get("name", "Standings")
    icon      = data.get("competition", {}).get("emblem")
    matchday  = (data.get("season") or {}).get("currentMatchday", "?")
    tables    = data.get("standings", [])

    embeds = []
    for table_block in tables:
        group_name  = table_block.get("group") or table_block.get("type", "")
        group_label = STAGE_NAMES.get(group_name, group_name.replace("_", " ").title())
        table       = table_block.get("table", [])

        rows = ["`#   Team                  P  W  D  L  GD  Pts`"]
        for row in table[:20]:
            pos  = str(row["position"]).rjust(2)
            team = (row["team"].get("shortName") or row["team"].get("name", ""))[:18].ljust(18)
            p    = str(row["playedGames"]).rjust(2)
            w    = str(row["won"]).rjust(2)
            d    = str(row["draw"]).rjust(2)
            l    = str(row["lost"]).rjust(2)
            gd   = str(row["goalDifference"]).rjust(3)
            pts  = str(row["points"]).rjust(3)
            rows.append(f"`{pos}. {team} {p} {w} {d} {l} {gd} {pts}`")

        title = f"🏆  {comp_name}  —  Matchday {matchday}"
        if group_label:
            title += f"\n{group_label}"

        em = discord.Embed(title=title, description="\n".join(rows), color=C_PURPLE)
        if icon:
            em.set_thumbnail(url=icon)
        _footer(em)
        embeds.append(em)
        if len(embeds) >= 3:
            break

    return embeds or [_footer(discord.Embed(title="No standings data", color=C_GREY))]


# ── Upcoming matches ───────────────────────────────────────────────────────────

def embed_upcoming(matches: list[dict], code: str, days: int) -> discord.Embed:
    comp_name = COMPETITION_CODES.get(code, code)
    em = discord.Embed(title=f"📆  {comp_name}  —  Next {days} days", color=C_BLUE)

    if not matches:
        em.description = "No upcoming matches found."
        return _footer(em)

    lines = []
    for m in matches[:20]:
        home, away, _ = _match_header(m)
        ko_dt = parse_dt(m.get("utcDate"))
        ts = _ts(ko_dt, "f") if ko_dt else "TBD"
        lines.append(f"**{home}  vs  {away}**\n{ts}  ·  {_match_label(m)}")

    em.description = "\n\n".join(lines)
    if len(matches) > 20:
        em.set_footer(text=f"Showing 20 of {len(matches)} matches  •  football-data.org")
    else:
        _footer(em)
    return em


# ── Competitions list ──────────────────────────────────────────────────────────

def embed_competitions() -> discord.Embed:
    em = discord.Embed(title="🌍  Supported Competitions", color=C_DARK_BLUE)
    lines = [f"`{code}` — {name}" for code, name in COMPETITION_CODES.items()]
    em.description = "\n".join(lines)
    em.add_field(name="Usage", value="`/standings WC` · `/upcoming CL 14`", inline=False)
    return _footer(em)


# ── Daily summary ──────────────────────────────────────────────────────────────

def embed_daily_summary(matches: list[dict], date_str: str) -> discord.Embed:
    finished = [m for m in matches if m.get("status") == "FINISHED"]
    em = discord.Embed(title=f"📊  Daily Summary  —  {date_str}", color=C_DARK_BLUE)

    if not finished:
        em.description = "No matches were completed today."
        return _footer(em)

    lines = []
    for m in finished:
        home, away, _ = _match_header(m)
        h, a   = get_score(m, "fullTime")
        score  = f"{h}–{a}" if h is not None else "?"
        lines.append(f"**{home}  {score}  {away}**  ·  {_match_label(m)}")

    em.description = "\n".join(lines)
    em.set_footer(text=f"{len(finished)} match{'es' if len(finished) != 1 else ''} completed  •  football-data.org")
    return em


# ── Startup ────────────────────────────────────────────────────────────────────

def embed_startup(user: discord.ClientUser) -> discord.Embed:
    em = discord.Embed(
        title="⚽  Kickoff Bot is Online!",
        description=(
            "World Cup & football match updates, reminders, and live notifications.\n\n"
            "**Quick setup:**\n"
            "1. `/setchannel` — live match events\n"
            "2. `/setpredictionschannel` — polls & MOTM voting\n"
            "3. `/setresultschannel` — results, points & leaderboard\n"
            "4. `/setsummarychannel` — recaps & highlights\n"
            "5. `/setcommandschannel` — command menu\n\n"
            "Type `/help` to see all commands."
        ),
        color=C_GREEN,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_footer(text=f"Bot: {user}")
    return em


# ── Next match ────────────────────────────────────────────────────────────────

def embed_nextmatch(match: dict) -> discord.Embed:
    home, away, _ = _match_header(match)
    ko_dt = parse_dt(match.get("utcDate"))
    venue = get_venue(match)

    em = discord.Embed(
        title="⏭️  Next Match",
        description=f"**{home}  vs  {away}**\n{_match_label(match)}",
        color=C_BLUE,
    )
    if ko_dt:
        em.add_field(name="Kick-off",  value=_ts(ko_dt, "f"), inline=True)
        em.add_field(name="Countdown", value=_ts(ko_dt, "R"), inline=True)
    if venue:
        em.add_field(name="Venue", value=venue, inline=False)
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Team profile ──────────────────────────────────────────────────────────────

def embed_team(team: dict, recent: list[dict], upcoming: list[dict]) -> discord.Embed:
    name    = team.get("name", "Unknown")
    area    = (team.get("area") or {}).get("name", "")
    cc      = (team.get("area") or {}).get("countryCode", "")
    flag    = flag_emoji(cc)
    crest   = team.get("crest") or None
    founded = team.get("founded")
    venue   = team.get("venue") or ""

    title = f"{flag} {name}".strip() if flag else name
    desc_parts = []
    if area:    desc_parts.append(f"🌍 {area}")
    if founded: desc_parts.append(f"📅 Founded {founded}")
    if venue:   desc_parts.append(f"🏟️ {venue}")

    em = discord.Embed(title=title, description="  ·  ".join(desc_parts), color=C_BLUE)
    if crest:
        em.set_thumbnail(url=crest)

    if recent:
        lines = []
        for m in recent[:5]:
            h, a = get_score(m, "fullTime")
            score = f"{h}–{a}" if h is not None else "?"
            home_t, away_t, _ = _match_header(m)
            lines.append(f"✅ **{home_t} {score} {away_t}**  ·  {_match_label(m)}")
        em.add_field(name="Recent Results", value="\n".join(lines) or "—", inline=False)

    if upcoming:
        lines = []
        for m in upcoming[:5]:
            home_t, away_t, _ = _match_header(m)
            ko = parse_dt(m.get("utcDate"))
            ts = _ts(ko, "d") if ko else "TBD"
            lines.append(f"🗓 {ts}  **{home_t} vs {away_t}**  ·  {_match_label(m)}")
        em.add_field(name="Upcoming Fixtures", value="\n".join(lines) or "—", inline=False)

    return _footer(em)


# ── WC group (single) ──────────────────────────────────────────────────────────

def embed_wc_group(group_letter: str, table: list[dict], comp_name: str = "FIFA World Cup") -> discord.Embed:
    rows = ["`#  Team                  P  W  D  L  GD  Pts`"]
    for row in table:
        pos = str(row["position"]).rjust(2)
        t   = (row["team"].get("shortName") or row["team"].get("name", ""))[:18].ljust(18)
        p   = str(row["playedGames"]).rjust(2)
        w   = str(row["won"]).rjust(2)
        d   = str(row["draw"]).rjust(2)
        l   = str(row["lost"]).rjust(2)
        gd  = str(row["goalDifference"]).rjust(3)
        pts = str(row["points"]).rjust(3)
        rows.append(f"`{pos}. {t} {p} {w} {d} {l} {gd} {pts}`")

    em = discord.Embed(
        title=f"🌍  Group {group_letter.upper()}",
        description="\n".join(rows),
        color=C_PURPLE,
    )
    return _footer(em)


# ── WC overview (all groups) ───────────────────────────────────────────────────

def embed_worldcup_overview(standings_data: dict) -> list[discord.Embed]:
    comp_name   = standings_data.get("competition", {}).get("name", "FIFA World Cup 2026")
    icon        = standings_data.get("competition", {}).get("emblem")
    tables      = standings_data.get("standings", [])
    group_tables = [t for t in tables if "GROUP" in (t.get("group") or "")]
    if not group_tables:
        group_tables = tables

    embeds = []
    for i in range(0, len(group_tables), 2):
        em = discord.Embed(title=f"🏆  {comp_name}  —  Group Stage", color=C_DARK_BLUE)
        if icon and i == 0:
            em.set_thumbnail(url=icon)

        for block in group_tables[i:i + 2]:
            group_raw = block.get("group", "")
            letter    = group_raw.replace("GROUP_", "") if group_raw else "?"
            table     = block.get("table", [])
            lines     = []
            for row in table[:4]:
                pos  = str(row["position"])
                team = (row["team"].get("shortName") or row["team"].get("name", ""))[:16]
                pts  = str(row["points"])
                gd   = str(row["goalDifference"])
                fl   = flag_emoji((row["team"].get("area") or {}).get("countryCode", ""))
                lines.append(f"`{pos}.` {fl} **{team}** — {pts} pts  *(GD {gd})*")
            em.add_field(name=f"Group {letter}", value="\n".join(lines) or "—", inline=True)

        _footer(em)
        embeds.append(em)

    return embeds or [_footer(discord.Embed(title="No World Cup data available", color=C_GREY))]


# ── Bracket (knockout stage) ───────────────────────────────────────────────────

def embed_bracket(matches: list[dict], comp_name: str = "FIFA World Cup 2026") -> list[discord.Embed]:
    stage_order = ["LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL"]
    by_stage: dict[str, list] = {s: [] for s in stage_order}
    for m in matches:
        s = m.get("stage", "")
        if s in by_stage:
            by_stage[s].append(m)

    embeds = []
    em = discord.Embed(title=f"🏆  {comp_name}  —  Knockout Bracket", color=C_GOLD)

    for stage_key in stage_order:
        ms = by_stage[stage_key]
        if not ms:
            continue
        stage_label = STAGE_NAMES.get(stage_key, stage_key)
        lines = []
        for m in ms:
            home_t, away_t, _ = _match_header(m)
            status  = m.get("status", "")
            num     = wc_match_number(m.get("id"))
            num_str = f"  *(Match {num})*" if num else ""
            if status == "FINISHED":
                h, a = get_score(m, "fullTime")
                lines.append(f"✅ **{home_t} {h}–{a} {away_t}**{num_str}")
            elif status in ("IN_PLAY", "PAUSED"):
                h, a = get_current_score(m)
                lines.append(f"🔴 **{home_t} {h}–{a} {away_t}** *(live)*{num_str}")
            else:
                ko = parse_dt(m.get("utcDate"))
                ts = _ts(ko, "d") if ko else "TBD"
                lines.append(f"🗓 {ts}  {home_t} vs {away_t}{num_str}")

        if len(em.fields) >= 24:
            embeds.append(_footer(em))
            em = discord.Embed(color=C_GOLD)
        em.add_field(name=stage_label, value="\n".join(lines), inline=False)

    if em.fields:
        embeds.append(_footer(em))
    return embeds or [_footer(discord.Embed(title="Knockout stage not yet available", color=C_GREY))]


# ── Help ───────────────────────────────────────────────────────────────────────

def embed_help() -> discord.Embed:
    em = discord.Embed(
        title="⚽  Kickoff Bot — Commands",
        description="All commands work as `/slash` or `!prefix`.",
        color=C_BLUE,
    )
    em.add_field(
        name="📅  Match Info",
        value=(
            "`/today` — Today's matches\n"
            "`/matchtoday [comp]` — Filter by competition\n"
            "`/live` — Currently live matches\n"
            "`/nextmatch [comp]` — Next upcoming match\n"
            "`/match <id>` — Match details by ID\n"
            "`/upcoming [comp] [days]` — Upcoming fixtures"
        ),
        inline=False,
    )
    em.add_field(
        name="🏆  Tournament",
        value=(
            "`/standings [comp]` — League / group standings\n"
            "`/group <A–P>` — World Cup group table\n"
            "`/worldcup` — All WC 2026 groups overview\n"
            "`/bracket` — WC 2026 knockout bracket"
        ),
        inline=False,
    )
    em.add_field(
        name="👥  Teams",
        value="`/team <name>` — Team profile, recent results & fixtures",
        inline=False,
    )
    em.add_field(
        name="🏅  Predictions",
        value=(
            "`/leaderboard` — Prediction leaderboard\n"
            "`/resetleaderboard` — Reset all scores (admin)"
        ),
        inline=False,
    )
    em.add_field(
        name="⚙️  Setup",
        value=(
            "`/setchannel` — Live match events\n"
            "`/setpredictionschannel` — Polls & MOTM voting\n"
            "`/setresultschannel` — Results, points & leaderboard\n"
            "`/setsummarychannel` — Recaps & highlights\n"
            "`/setcommandschannel` — Command menu\n"
            "`/setmode` — Guild notification verbosity\n"
            "`/settimezone` — Server timezone\n"
            "`/status` — Bot config for this server"
        ),
        inline=False,
    )
    em.add_field(
        name="🔧  Other",
        value=(
            "`/competitions` — Supported competition codes\n"
            "`/updatecommandsmenu` — Rebuild commands channel menu\n"
            "`/testembed <type>` — Preview embed (admin)\n"
            "`/help` — This message"
        ),
        inline=False,
    )
    return _footer(em)


# ── Test embed (admin only) ────────────────────────────────────────────────────

def _mock_match() -> dict:
    from datetime import timedelta
    ko = datetime.now(timezone.utc) + timedelta(hours=1)
    return {
        "id": 999999,
        "competition": {"name": "FIFA World Cup 2026", "code": "WC", "emblem": None},
        "homeTeam": {"id": 1, "name": "Brazil",    "shortName": "Brazil",    "area": {"countryCode": "BR"}},
        "awayTeam": {"id": 2, "name": "Argentina", "shortName": "Argentina", "area": {"countryCode": "AR"}},
        "stage": "SEMI_FINALS",
        "group": None,
        "venue": "MetLife Stadium, New Jersey",
        "utcDate": ko.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "IN_PLAY",
        "score": {
            "fullTime": {"home": 2, "away": 1},
            "halfTime": {"home": 1, "away": 0},
            "extraTime": {"home": None, "away": None},
            "penalties": {"home": None, "away": None},
        },
        "goals": [
            {"minute": 23, "scorer": {"id": 101, "name": "Vinicius Jr."},     "assist": {"id": 103, "name": "Rodrygo"},
             "team": {"id": 1}, "type": "REGULAR"},
            {"minute": 55, "scorer": {"id": 201, "name": "Lautaro Martínez"}, "assist": None,
             "team": {"id": 2}, "type": "REGULAR"},
            {"minute": 78, "scorer": {"id": 102, "name": "Rodrygo"},          "assist": {"id": 101, "name": "Vinicius Jr."},
             "team": {"id": 1}, "type": "REGULAR"},
        ],
        "bookings": [
            {"minute": 61, "player": {"id": 202, "name": "De Paul"},   "team": {"id": 2}, "card": "YELLOW"},
            {"minute": 87, "player": {"id": 301, "name": "Otamendi"}, "team": {"id": 2}, "card": "RED"},
        ],
        "statistics": [
            {"type": "ball_possession",  "home": "58%", "away": "42%"},
            {"type": "total_shots",      "home": 12,    "away": 8},
            {"type": "shots_on_target",  "home": 5,     "away": 3},
            {"type": "corner_kicks",     "home": 7,     "away": 3},
            {"type": "fouls_committed",  "home": 9,     "away": 14},
        ],
        "homeTeam": {
            "id": 1, "name": "Brazil", "shortName": "Brazil",
            "area": {"countryCode": "BR"},
            "formation": "4-3-3",
            "lineup": [
                {"shirtNumber": 1,  "name": "Alisson",       "position": "GK"},
                {"shirtNumber": 2,  "name": "Danilo",        "position": "DEF"},
                {"shirtNumber": 4,  "name": "Marquinhos",    "position": "DEF"},
                {"shirtNumber": 3,  "name": "Bremer",        "position": "DEF"},
                {"shirtNumber": 6,  "name": "Alex Sandro",   "position": "DEF"},
                {"shirtNumber": 5,  "name": "Casemiro",      "position": "MID"},
                {"shirtNumber": 8,  "name": "Fred",          "position": "MID"},
                {"shirtNumber": 17, "name": "Lucas Paquetá", "position": "MID"},
                {"shirtNumber": 11, "name": "Rodrygo",       "position": "FWD"},
                {"shirtNumber": 9,  "name": "Richarlison",   "position": "FWD"},
                {"shirtNumber": 20, "name": "Vinicius Jr.",  "position": "FWD"},
            ],
        },
        "awayTeam": {
            "id": 2, "name": "Argentina", "shortName": "Argentina",
            "area": {"countryCode": "AR"},
            "formation": "4-4-2",
            "lineup": [
                {"shirtNumber": 23, "name": "E. Martínez",       "position": "GK"},
                {"shirtNumber": 26, "name": "Nahuel Molina",     "position": "DEF"},
                {"shirtNumber": 13, "name": "Cristian Romero",   "position": "DEF"},
                {"shirtNumber": 19, "name": "N. Otamendi",       "position": "DEF"},
                {"shirtNumber": 8,  "name": "Marcos Acuña",      "position": "DEF"},
                {"shirtNumber": 7,  "name": "Rodrigo De Paul",   "position": "MID"},
                {"shirtNumber": 5,  "name": "Leandro Paredes",   "position": "MID"},
                {"shirtNumber": 20, "name": "Alexis Mac Allister","position": "MID"},
                {"shirtNumber": 11, "name": "Ángel Di María",    "position": "MID"},
                {"shirtNumber": 22, "name": "Lautaro Martínez",  "position": "FWD"},
                {"shirtNumber": 10, "name": "Lionel Messi",      "position": "FWD"},
            ],
        },
    }


def embed_test(embed_type: str) -> discord.Embed:
    m = _mock_match()
    t = embed_type.lower().strip()

    if t == "reminder15":     return embed_reminder(m, 15)
    if t in ("kickoff","ko"): return embed_kickoff(m)
    if t == "lineup":         return embed_lineup(m, m)
    if t == "goal":           return embed_goal(m, m, m["goals"][0])
    if t in ("redcard","red"):
        rc = next(b for b in m["bookings"] if b["card"] == "RED")
        return embed_red_card(m, m, rc)
    if t in ("halftime","ht"):      return embed_halftime(m, m)
    if t in ("secondhalf","2h"):    return embed_second_half(m)
    if t in ("extratime","et"):     return embed_extra_time(m, m)
    if t in ("pso","penalties"):    return embed_penalty_shootout(m, m)
    if t in ("fulltime","ft"):
        m["status"] = "FINISHED"
        return embed_fulltime(m, m)
    if t == "motm":
        return embed_motm_vote(m, ["Vinicius Jr.", "Rodrygo", "Lautaro Martínez", "Messi"])
    if t == "recap":
        yt = {"title": "Brazil 2-1 Argentina | Semi-Final Highlights", "url": "https://youtube.com/watch?v=test",
              "channel": "FIFA", "thumbnail": ""}
        return embed_full_recap(m, m, yt)
    if t == "prediction":
        return embed_prediction_prompt(m)

    em = discord.Embed(
        title="❓  Unknown embed type",
        description=(
            "Valid: `reminder15` `kickoff` `lineup` `goal` `redcard` `halftime`\n"
            "`secondhalf` `extratime` `pso` `fulltime` `motm` `recap` `prediction`"
        ),
        color=C_GREY,
    )
    return _footer(em)
