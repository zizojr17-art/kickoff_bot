"""
All Discord embed builders — one function per notification type.

Design rules:
- WC matches: suppress "FIFA World Cup 2026" from every embed.
  Show  Stage  ·  Match N  instead (e.g. "Group A  ·  Match 5").
- Non-WC matches: show competition name + stage as before.
- All kick-off times use Discord <t:…> timestamps so every user
  sees the time in their own local timezone automatically.
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

def _footer(embed: discord.Embed) -> discord.Embed:
    embed.set_footer(text="football-data.org • Kickoff Bot")
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
        letter = raw_group.replace("GROUP_", "")
        return f"Group {letter}"
    return get_stage(match)


def _match_label(match: dict) -> str:
    """
    For WC matches → 'Group A  ·  Match 5'  (no competition name shown)
    For other matches → 'UEFA Champions League  ·  Quarter-finals'
    """
    if _is_wc(match):
        stage = _stage_label(match)
        num = wc_match_number(match.get("id"))
        if num:
            return f"{stage}  ·  Match {num}"
        return stage
    # Non-WC
    comp  = match.get("competition", {}).get("name", "Football")
    stage = _stage_label(match)
    return f"{comp}  ·  {stage}"


# ── Pre-match reminders ───────────────────────────────────────────────────────

def embed_reminder(match: dict, mins_before: int) -> discord.Embed:
    home, away, _ = _match_header(match)
    ko_dt = parse_dt(match.get("utcDate"))
    venue = get_venue(match)
    label = _match_label(match)

    if mins_before >= 60:
        title = "⏰  1 Hour to Kick-off"
        color = C_BLUE
    else:
        title = "🔔  15 Minutes to Kick-off"
        color = C_ORANGE

    em = discord.Embed(
        title=title,
        description=f"**{home}  vs  {away}**\n{label}",
        color=color,
    )
    if ko_dt:
        em.add_field(name="Kick-off", value=_ts(ko_dt, "R"), inline=True)
        em.add_field(name="Local time", value=_ts(ko_dt, "t"), inline=True)
    if venue:
        em.add_field(name="Venue", value=venue, inline=False)
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Prediction poll ────────────────────────────────────────────────────────────

def embed_prediction_poll(match: dict) -> discord.Embed:
    home, away, _ = _match_header(match)
    ko_dt = parse_dt(match.get("utcDate"))
    label = _match_label(match)

    em = discord.Embed(
        title="🗳️  Match Prediction",
        description=(
            f"**{home}  vs  {away}**\n"
            f"{label}"
            + (f"\nKick-off {_ts(ko_dt, 'R')}" if ko_dt else "")
        ),
        color=C_GOLD,
    )
    em.add_field(
        name="Cast your vote!",
        value="1️⃣  Home win\n🤝  Draw\n2️⃣  Away win",
        inline=False,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


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

    card_type = (booking or {}).get("card", "RED")
    card_label = "🟥  RED CARD" if card_type == "RED" else "🟨🟥  SECOND YELLOW"

    player = (booking or {}).get("player", {}) or {}
    player_name = player.get("name", "")
    minute = (booking or {}).get("minute")
    team_id = ((booking or {}).get("team") or {}).get("id")
    home_id = match.get("homeTeam", {}).get("id")
    team_name = home if team_id == home_id else away

    desc = f"**{home}  {score_str}  {away}**" if score_str else f"**{home}  vs  {away}**"
    desc += f"\n{label}"

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
    _add_stats(em, detail)
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Second half ───────────────────────────────────────────────────────────────

def embed_second_half(match: dict) -> discord.Embed:
    home, away, _ = _match_header(match)
    label = _match_label(match)
    em = discord.Embed(
        title="▶️  SECOND HALF UNDERWAY",
        description=f"**{home}  vs  {away}**\n{label}",
        color=C_BLUE,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Extra time ────────────────────────────────────────────────────────────────

def embed_extra_time(match: dict, detail: Optional[dict] = None) -> discord.Embed:
    home, away, _ = _match_header(match)
    d = detail or match
    h, a = get_current_score(d)
    score_str = f"{h}–{a}" if h is not None else "? – ?"
    label = _match_label(match)

    em = discord.Embed(
        title="⏱️  EXTRA TIME",
        description=f"**{home}  {score_str}  {away}**\n{label}",
        color=C_PURPLE,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Penalty shootout ──────────────────────────────────────────────────────────

def embed_penalty_shootout(match: dict, detail: Optional[dict] = None) -> discord.Embed:
    home, away, _ = _match_header(match)
    label = _match_label(match)

    em = discord.Embed(
        title="🎯  PENALTY SHOOTOUT!",
        description=f"**{home}  vs  {away}**\n{label}",
        color=C_PURPLE,
    )
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


# ── Full-time ─────────────────────────────────────────────────────────────────

def embed_fulltime(match: dict, detail: Optional[dict] = None) -> discord.Embed:
    home, away, _ = _match_header(match)
    d = detail or match
    h, a = get_score(d, "fullTime")
    if h is None:
        h, a = get_current_score(d)
    score_str = f"{h}–{a}" if h is not None else "? – ?"
    label = _match_label(match)

    result_line = ""
    if h is not None and a is not None:
        home_name = match.get("homeTeam", {}).get("shortName") or match.get("homeTeam", {}).get("name", "Home")
        away_name = match.get("awayTeam", {}).get("shortName") or match.get("awayTeam", {}).get("name", "Away")
        if h > a:
            result_line = f"🏆 **{home_name}** win"
        elif a > h:
            result_line = f"🏆 **{away_name}** win"
        else:
            result_line = "🤝 Draw"

    pen_h, pen_a = penalty_scores(d)
    pen_line = ""
    if pen_h is not None and pen_a is not None:
        pen_winner = match.get("homeTeam", {}).get("shortName") or home
        if pen_a > pen_h:
            pen_winner = match.get("awayTeam", {}).get("shortName") or away
        pen_line = f"Penalties: {pen_h}–{pen_a}  ·  {pen_winner} advance"

    desc = f"**{home}  {score_str}  {away}**\n{label}"
    if result_line:
        desc += f"\n{result_line}"
    if pen_line:
        desc += f"\n{pen_line}"

    em = discord.Embed(title="🏁  FULL TIME", description=desc, color=C_GREEN)
    _add_stats(em, detail)
    if icon := _comp_icon(match):
        em.set_thumbnail(url=icon)
    return _footer(em)


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
            ko_dt = parse_dt(m.get("utcDate"))

            if status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
                h, a = get_current_score(m)
                score = f"{h}–{a}" if h is not None else "?"
                st = {"IN_PLAY": "🔴 LIVE", "PAUSED": "⏸ HT", "EXTRA_TIME": "⏱ ET", "PENALTY_SHOOTOUT": "🎯 PSO"}.get(status, status)
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
                field_name = comp if i == 0 else f"{comp} (cont.)"
                if len(em.fields) >= 25:
                    embeds.append(_footer(em))
                    em = discord.Embed(color=C_BLUE)
                em.add_field(name=field_name, value=c, inline=False)

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
    icon = data.get("competition", {}).get("emblem")
    matchday = (data.get("season") or {}).get("currentMatchday", "?")
    tables = data.get("standings", [])

    embeds = []
    for table_block in tables:
        group_name = table_block.get("group") or table_block.get("type", "")
        group_label = STAGE_NAMES.get(group_name, group_name.replace("_", " ").title())
        table = table_block.get("table", [])

        rows = ["`#   Team                  P  W  D  L  GD  Pts`"]
        for row in table[:20]:
            pos = str(row["position"]).rjust(2)
            team = (row["team"].get("shortName") or row["team"].get("name", ""))[:18].ljust(18)
            p  = str(row["playedGames"]).rjust(2)
            w  = str(row["won"]).rjust(2)
            d  = str(row["draw"]).rjust(2)
            l  = str(row["lost"]).rjust(2)
            gd = str(row["goalDifference"]).rjust(3)
            pts = str(row["points"]).rjust(3)
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
        stage = _match_label(m)
        ts = _ts(ko_dt, "f") if ko_dt else "TBD"
        lines.append(f"**{home}  vs  {away}**\n{ts}  ·  {stage}")

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
    em.add_field(name="Usage", value="`/standings WC` · `/upcoming CL 14` · `!standings PL`", inline=False)
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
        home, away, comp = _match_header(m)
        h, a = get_score(m, "fullTime")
        score = f"{h}–{a}" if h is not None else "?"
        label = _match_label(m)
        lines.append(f"**{home}  {score}  {away}**  ·  {label}")

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
            "1. Run `/setchannel` in your preferred channel — notifications start immediately.\n"
            "2. Optionally run `/setmode` (quiet / detailed) and `/settimezone`.\n\n"
            "Type `/help` to see all available commands."
        ),
        color=C_GREEN,
        timestamp=datetime.now(timezone.utc),
    )
    em.set_footer(text=f"Bot: {user}")
    return em


# ── Next match ────────────────────────────────────────────────────────────────

def embed_nextmatch(match: dict) -> discord.Embed:
    home, away, _ = _match_header(match)
    ko_dt  = parse_dt(match.get("utcDate"))
    label  = _match_label(match)
    venue  = get_venue(match)

    em = discord.Embed(
        title="⏭️  Next Match",
        description=f"**{home}  vs  {away}**\n{label}",
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

def embed_team(
    team: dict,
    recent: list[dict],
    upcoming: list[dict],
) -> discord.Embed:
    name    = team.get("name", "Unknown")
    area    = (team.get("area") or {}).get("name", "")
    cc      = (team.get("area") or {}).get("countryCode", "")
    flag    = flag_emoji(cc)
    crest   = team.get("crest") or None
    founded = team.get("founded")
    venue   = team.get("venue") or ""

    title = f"{flag} {name}".strip() if flag else name
    desc_parts = []
    if area:
        desc_parts.append(f"🌍 {area}")
    if founded:
        desc_parts.append(f"📅 Founded {founded}")
    if venue:
        desc_parts.append(f"🏟️ {venue}")

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
        pos  = str(row["position"]).rjust(2)
        t    = (row["team"].get("shortName") or row["team"].get("name", ""))[:18].ljust(18)
        p    = str(row["playedGames"]).rjust(2)
        w    = str(row["won"]).rjust(2)
        d    = str(row["draw"]).rjust(2)
        l    = str(row["lost"]).rjust(2)
        gd   = str(row["goalDifference"]).rjust(3)
        pts  = str(row["points"]).rjust(3)
        rows.append(f"`{pos}. {t} {p} {w} {d} {l} {gd} {pts}`")

    em = discord.Embed(
        title=f"🌍  Group {group_letter.upper()}",
        description="\n".join(rows),
        color=C_PURPLE,
    )
    return _footer(em)


# ── WC overview (all groups) ───────────────────────────────────────────────────

def embed_worldcup_overview(standings_data: dict) -> list[discord.Embed]:
    comp_name = standings_data.get("competition", {}).get("name", "FIFA World Cup 2026")
    icon      = standings_data.get("competition", {}).get("emblem")
    tables    = standings_data.get("standings", [])

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

            lines = []
            for row in table[:4]:
                pos  = str(row["position"])
                team = (row["team"].get("shortName") or row["team"].get("name", ""))[:16]
                pts  = str(row["points"])
                gd   = str(row["goalDifference"])
                flag = flag_emoji((row["team"].get("area") or {}).get("countryCode", ""))
                lines.append(f"`{pos}.` {flag} **{team}** — {pts} pts  *(GD {gd})*")

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
            status = m.get("status", "")
            num = wc_match_number(m.get("id"))
            num_str = f"  *(Match {num})*" if num else ""
            if status == "FINISHED":
                h, a = get_score(m, "fullTime")
                score = f"{h}–{a}" if h is not None else "?"
                lines.append(f"✅ **{home_t} {score} {away_t}**{num_str}")
            elif status in ("IN_PLAY", "PAUSED"):
                h, a = get_current_score(m)
                score = f"{h}–{a}" if h is not None else "?"
                lines.append(f"🔴 **{home_t} {score} {away_t}** *(live)*{num_str}")
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
            "`/today` — Today's matches across all competitions\n"
            "`/matchtoday [comp]` — Today's matches, filter by competition\n"
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
        name="⚙️  Setup",
        value=(
            "`/setchannel` — Set this channel for automatic notifications\n"
            "`/setmode <quiet|detailed>` — Notification verbosity\n"
            "`/settimezone <TZ>` — Server timezone for daily summary\n"
            "`/timezone` — Show current timezone\n"
            "`/status` — Bot configuration for this server"
        ),
        inline=False,
    )
    em.add_field(
        name="🔧  Other",
        value=(
            "`/competitions` — List supported competition codes\n"
            "`/testembed <type>` — Preview a notification embed (admin)\n"
            "`/help` — This message"
        ),
        inline=False,
    )
    em.add_field(
        name="📌  Notification setup",
        value=(
            "1. Run `/setchannel` — monitoring starts automatically.\n"
            "2. Optionally `/setmode detailed` for extra events.\n"
            "3. Optionally `/settimezone America/New_York` for daily summary timing."
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
            {"minute": 23, "scorer": {"id": 101, "name": "Vinicius Jr."},       "team": {"id": 1}, "type": "REGULAR"},
            {"minute": 55, "scorer": {"id": 201, "name": "Lautaro Martínez"},   "team": {"id": 2}, "type": "REGULAR"},
            {"minute": 78, "scorer": {"id": 102, "name": "Rodrygo"},            "team": {"id": 1}, "type": "REGULAR"},
        ],
        "bookings": [
            {"minute": 61, "player": {"id": 202, "name": "De Paul"},   "team": {"id": 2}, "card": "YELLOW"},
            {"minute": 87, "player": {"id": 301, "name": "Otamendi"}, "team": {"id": 2}, "card": "RED"},
        ],
        "statistics": [
            {"type": "ball_possession", "home": "58%", "away": "42%"},
            {"type": "total_shots",     "home": 12,    "away": 8},
            {"type": "shots_on_target", "home": 5,     "away": 3},
        ],
    }


def embed_test(embed_type: str) -> discord.Embed:
    """Return a realistic test embed for admin preview."""
    m = _mock_match()
    t = embed_type.lower().strip()

    if t == "reminder60":
        return embed_reminder(m, 60)
    if t == "reminder15":
        return embed_reminder(m, 15)
    if t in ("kickoff", "ko"):
        return embed_kickoff(m)
    if t == "goal":
        return embed_goal(m, m, m["goals"][0])
    if t in ("redcard", "red"):
        rc = next(b for b in m["bookings"] if b["card"] == "RED")
        return embed_red_card(m, m, rc)
    if t in ("halftime", "ht"):
        return embed_halftime(m, m)
    if t in ("secondhalf", "2h"):
        return embed_second_half(m)
    if t in ("extratime", "et"):
        return embed_extra_time(m, m)
    if t in ("pso", "penalties"):
        return embed_penalty_shootout(m, m)
    if t in ("fulltime", "ft"):
        m["status"] = "FINISHED"
        return embed_fulltime(m, m)
    if t == "poll":
        return embed_prediction_poll(m)

    em = discord.Embed(
        title="❓  Unknown embed type",
        description=(
            "Valid types: `reminder60` `reminder15` `kickoff` `goal` `redcard`\n"
            "`halftime` `secondhalf` `extratime` `pso` `fulltime` `poll`"
        ),
        color=C_GREY,
    )
    return _footer(em)


# ── Internal stats helper ──────────────────────────────────────────────────────

def _add_stats(em: discord.Embed, detail: Optional[dict]) -> None:
    """Append match stats if available."""
    if not detail:
        return
    stats = detail.get("statistics") or []
    if not stats:
        return

    def _find(label: str) -> tuple[str, str]:
        for s in stats:
            if label.lower() in s.get("type", "").lower():
                home = s.get("home") or s.get("homeTeam") or ""
                away = s.get("away") or s.get("awayTeam") or ""
                return str(home), str(away)
        return "", ""

    pos_h,   pos_a   = _find("possession")
    sot_h,   sot_a   = _find("shots on target")
    shots_h, shots_a = _find("shots")

    lines = []
    if pos_h or pos_a:
        lines.append(f"Possession  {pos_h} — {pos_a}")
    if shots_h or shots_a:
        lines.append(f"Shots  {shots_h} — {shots_a}")
    if sot_h or sot_a:
        lines.append(f"Shots on target  {sot_h} — {sot_a}")

    if lines:
        em.add_field(name="Stats", value="\n".join(lines), inline=False)
