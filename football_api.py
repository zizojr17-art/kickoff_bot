"""
football_api.py — football-data.org API wrapper, World Cup 2026 only.
All public functions are async. The session is created on first use and
reused across calls.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

import aiohttp

log = logging.getLogger("football_api")

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL = "https://api.football-data.org/v4"


def _current_api_key() -> str:
    return os.environ.get("FOOTBALL_DATA_API_KEY", "")


WC_CODE = "WC"

# ── WC 2026 national teams (48 qualified nations) ────────────────────────────

WC_TEAMS: list[str] = [
    # CONCACAF (confirmed hosts + qualifiers)
    "Canada", "Mexico", "United States",
    # UEFA (16 spots)
    "Germany", "France", "Spain", "England", "Portugal", "Netherlands",
    "Belgium", "Italy", "Croatia", "Serbia", "Switzerland", "Austria",
    "Denmark", "Poland", "Türkiye", "Slovakia", "Scotland", "Hungary",
    "Ukraine", "Slovenia", "Romania", "Czech Republic", "Albania",
    "Georgia", "Greece",
    # CONMEBOL (6 spots)
    "Argentina", "Brazil", "Colombia", "Ecuador", "Uruguay", "Venezuela",
    # CAF (9 spots)
    "Morocco", "Senegal", "Egypt", "Nigeria", "Cameroon", "Ghana",
    "Côte d'Ivoire", "South Africa", "Tunisia",
    # AFC (8 spots)
    "Japan", "South Korea", "Iran", "Saudi Arabia", "Australia",
    "Qatar", "Iraq", "Jordan",
    # OFC
    "New Zealand",
    # CONCACAF qualifiers
    "Panama", "Costa Rica", "Jamaica", "Honduras",
]

WC_GROUPS: list[str] = [chr(c) for c in range(ord("A"), ord("Q"))]  # A–P

STAGE_NAMES: dict[str, str] = {
    "GROUP_STAGE":      "Group Stage",
    "LAST_16":          "Round of 16",
    "QUARTER_FINALS":   "Quarter-finals",
    "SEMI_FINALS":      "Semi-finals",
    "THIRD_PLACE":      "Third Place Play-off",
    "FINAL":            "Final",
}

# ── HTTP session ──────────────────────────────────────────────────────────────

_session: aiohttp.ClientSession | None = None
_session_key: str = ""          # API key the current session was created with


def _get_session() -> aiohttp.ClientSession:
    """Return a cached aiohttp session, recreating it if the API key changed."""
    global _session, _session_key
    key = _current_api_key()
    if _session is None or _session.closed or key != _session_key:
        # Close stale session without awaiting (best-effort)
        if _session and not _session.closed:
            try:
                _session._connector.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        timeout  = aiohttp.ClientTimeout(total=15)
        _session = aiohttp.ClientSession(headers={"X-Auth-Token": key}, timeout=timeout)
        _session_key = key
    return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


# ── Core HTTP helper ──────────────────────────────────────────────────────────

async def _get(path: str, params: dict | None = None) -> dict | list | None:
    url = f"{BASE_URL}{path}"
    try:
        async with _get_session().get(url, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 429:
                log.warning("[API] Rate limited (429) on %s — backing off 60 s", path)
                await asyncio.sleep(60)
                return None
            if resp.status in (401, 403):
                log.error("[API] Auth error %d on %s — check FOOTBALL_DATA_API_KEY", resp.status, path)
                return None
            log.warning("[API] HTTP %d on %s", resp.status, path)
            return None
    except aiohttp.ClientConnectorError:
        log.error("[API] Connection failed — network issue")
        return None
    except asyncio.TimeoutError:
        log.error("[API] Timeout on %s", path)
        return None
    except Exception as exc:
        log.error("[API] Unexpected error on %s: %s", path, exc)
        return None


# ── Match data ─────────────────────────────────────────────────────────────────

async def get_todays_matches() -> list[dict]:
    """All World Cup matches today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data  = await _get(f"/competitions/{WC_CODE}/matches", {"dateFrom": today, "dateTo": today})
    if not data:
        return []
    return data.get("matches", [])


async def get_live_matches() -> list[dict]:
    """Currently live World Cup matches."""
    data = await _get(f"/competitions/{WC_CODE}/matches", {"status": "LIVE"})
    if not data:
        return []
    live = data.get("matches", [])
    # Also check IN_PLAY, PAUSED, EXTRA_TIME, PENALTY_SHOOTOUT
    data2 = await _get(f"/competitions/{WC_CODE}/matches", {
        "status": "IN_PLAY,PAUSED,EXTRA_TIME,PENALTY_SHOOTOUT"
    })
    if data2:
        for m in data2.get("matches", []):
            if not any(x["id"] == m["id"] for x in live):
                live.append(m)
    return live


async def get_match_detail(match_id: int) -> dict | None:
    """Full detail for one match including goals and bookings."""
    return await _get(f"/matches/{match_id}")


async def get_competition_matches(
    date_from: str,
    date_to: str,
    status: str | None = None,
) -> list[dict]:
    """World Cup matches within a date range."""
    params: dict[str, Any] = {"dateFrom": date_from, "dateTo": date_to}
    if status:
        params["status"] = status
    data = await _get(f"/competitions/{WC_CODE}/matches", params)
    return data.get("matches", []) if data else []


async def get_next_match() -> dict | None:
    """Next scheduled World Cup match."""
    today  = datetime.now(timezone.utc)
    end    = (today + timedelta(days=7)).strftime("%Y-%m-%d")
    data   = await _get(f"/competitions/{WC_CODE}/matches", {
        "dateFrom": today.strftime("%Y-%m-%d"),
        "dateTo":   end,
        "status":   "SCHEDULED,TIMED",
    })
    matches = data.get("matches", []) if data else []
    if not matches:
        return None
    matches.sort(key=lambda m: m.get("utcDate", ""))
    return matches[0]


async def get_standings() -> dict | None:
    """World Cup group standings."""
    return await _get(f"/competitions/{WC_CODE}/standings")


async def get_scorers(limit: int = 10) -> list[dict]:
    """Top scorers in the World Cup."""
    data = await _get(f"/competitions/{WC_CODE}/scorers", {"limit": limit})
    return data.get("scorers", []) if data else []


# ── Team data ─────────────────────────────────────────────────────────────────

async def search_team(name: str) -> dict | None:
    """Search for a WC team by name (fuzzy match)."""
    name_lower = name.strip().lower()
    # Check against known WC teams first
    for team_name in WC_TEAMS:
        if name_lower in team_name.lower():
            data = await _get("/teams", {"search": team_name})
            if data:
                teams = data.get("teams", [])
                if teams:
                    return teams[0]
    # Fall back to API search
    data = await _get("/teams", {"search": name})
    if not data:
        return None
    teams = data.get("teams", [])
    return teams[0] if teams else None


async def get_team(team_id: int) -> dict | None:
    return await _get(f"/teams/{team_id}")


async def get_team_matches(
    team_id: int,
    status: str = "FINISHED",
    limit: int = 5,
) -> list[dict]:
    params = {"status": status, "competitions": WC_CODE, "limit": limit}
    data   = await _get(f"/teams/{team_id}/matches", params)
    return data.get("matches", []) if data else []


# ── WC match ordering ─────────────────────────────────────────────────────────

_wc_match_order: list[int] = []


async def load_wc_match_order() -> None:
    global _wc_match_order
    today   = datetime.now(timezone.utc)
    end     = (today + timedelta(days=90)).strftime("%Y-%m-%d")
    matches = await get_competition_matches("2026-06-01", end)
    _wc_match_order = [m["id"] for m in matches]
    log.info("[API] Loaded %d WC match IDs", len(_wc_match_order))


def get_wc_match_order() -> list[int]:
    return _wc_match_order


# ── YouTube highlights ─────────────────────────────────────────────────────────

async def search_youtube_highlights(query: str) -> str | None:
    """Return a YouTube search URL (no API key required for search link)."""
    encoded = query.replace(" ", "+").replace("/", "+")
    return f"https://www.youtube.com/results?search_query={encoded}+highlights"


# ── Lineup helpers ────────────────────────────────────────────────────────────

def has_confirmed_lineups(detail: dict) -> bool:
    lineups = detail.get("lineups", [])
    return len(lineups) >= 2 and all(
        len(l.get("startXI", [])) > 0 for l in lineups
    )


# ── Score / event helpers ─────────────────────────────────────────────────────

def parse_dt(dt_str: str) -> datetime | None:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_score(match: dict, period: str = "fullTime") -> tuple[int | None, int | None]:
    score = match.get("score", {})
    h     = (score.get(period) or {}).get("home")
    a     = (score.get(period) or {}).get("away")
    return h, a


def get_current_score(match: dict) -> tuple[int, int]:
    for period in ("regularTime", "extraTime", "penalties", "fullTime", "halfTime"):
        h, a = get_score(match, period)
        if h is not None and a is not None:
            return h, a
    return 0, 0


def goal_key(goal: dict) -> str:
    minute = goal.get("minute") or 0
    scorer = (goal.get("scorer") or {}).get("id", 0)
    team   = (goal.get("team") or {}).get("id", 0)
    return f"{team}:{scorer}:{minute}"


def card_key(card: dict) -> str:
    minute = card.get("minute") or 0
    player = (card.get("player") or {}).get("id", 0)
    return f"{player}:{minute}"


def is_knockout(match: dict) -> bool:
    stage = match.get("stage", "")
    return stage in ("LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")


def team_display(team: dict) -> str:
    return team.get("shortName") or team.get("name") or "TBD"


def team_flag(team: dict) -> str:
    """Return a flag emoji for common WC nations."""
    flags = {
        "Argentina":   "🇦🇷", "Australia":  "🇦🇺", "Austria":   "🇦🇹",
        "Belgium":     "🇧🇪", "Brazil":     "🇧🇷", "Cameroon":  "🇨🇲",
        "Canada":      "🇨🇦", "Colombia":   "🇨🇴", "Costa Rica":"🇨🇷",
        "Croatia":     "🇭🇷", "Czech Republic": "🇨🇿",
        "Denmark":     "🇩🇰", "Ecuador":   "🇪🇨", "Egypt":     "🇪🇬",
        "England":     "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "France":    "🇫🇷", "Germany":   "🇩🇪",
        "Ghana":       "🇬🇭", "Greece":    "🇬🇷", "Honduras":  "🇭🇳",
        "Hungary":     "🇭🇺", "Iran":      "🇮🇷", "Iraq":      "🇮🇶",
        "Italy":       "🇮🇹", "Jamaica":   "🇯🇲", "Japan":     "🇯🇵",
        "Jordan":      "🇯🇴", "Mexico":    "🇲🇽", "Morocco":   "🇲🇦",
        "Netherlands": "🇳🇱", "New Zealand":"🇳🇿", "Nigeria":  "🇳🇬",
        "Panama":      "🇵🇦", "Poland":    "🇵🇱", "Portugal":  "🇵🇹",
        "Qatar":       "🇶🇦", "Romania":   "🇷🇴", "Saudi Arabia": "🇸🇦",
        "Scotland":    "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "Senegal":   "🇸🇳", "Serbia":   "🇷🇸",
        "Slovakia":    "🇸🇰", "Slovenia":  "🇸🇮", "South Africa": "🇿🇦",
        "South Korea": "🇰🇷", "Spain":     "🇪🇸", "Switzerland":"🇨🇭",
        "Tunisia":     "🇹🇳", "Türkiye":   "🇹🇷", "Ukraine":   "🇺🇦",
        "United States": "🇺🇸", "Uruguay": "🇺🇾", "Venezuela": "🇻🇪",
        "Albania":     "🇦🇱", "Georgia":   "🇬🇪", "Côte d'Ivoire": "🇨🇮",
    }
    name = team.get("name") or team.get("shortName") or ""
    return flags.get(name, "🏳️")


# ── Player search ──────────────────────────────────────────────────────────────

async def search_player(name: str, scorer_limit: int = 100) -> dict | None:
    """
    Search for a player by name in the WC scorers list.
    Returns the scorer entry dict (player, team, goals, assists, penalties) or None.
    """
    data = await _get(f"/competitions/{WC_CODE}/scorers", {"limit": scorer_limit})
    scorers = data.get("scorers", []) if data else []
    name_lower = name.strip().lower()
    for entry in scorers:
        player_name = (entry.get("player") or {}).get("name", "")
        if name_lower in player_name.lower():
            return entry
    return None


async def get_player_wc_stats(name: str) -> dict | None:
    """
    Return a player's WC stats from the scorers list, or None if not found.
    Entry shape: { player: {id, name, nationality}, team: {...}, goals, assists, penalties }
    """
    return await search_player(name)


# ── Match stats ────────────────────────────────────────────────────────────────

async def get_match_stats(match_id: int) -> dict | None:
    """
    Return an enriched stats dict for a finished match, derived from match detail.
    """
    detail = await get_match_detail(match_id)
    if not detail:
        return None

    goals     = detail.get("goals")         or []
    bookings  = detail.get("bookings")      or []
    subs      = detail.get("substitutions") or []
    lineups   = detail.get("lineups")       or []

    home_id = (detail.get("homeTeam") or {}).get("id")
    away_id = (detail.get("awayTeam") or {}).get("id")

    def _team_id(entry: dict) -> int | None:
        return (entry.get("team") or {}).get("id")

    home_goals    = [g for g in goals    if _team_id(g) == home_id]
    away_goals    = [g for g in goals    if _team_id(g) == away_id]
    home_yellows  = [b for b in bookings if _team_id(b) == home_id and b.get("card") == "YELLOW"]
    away_yellows  = [b for b in bookings if _team_id(b) == away_id and b.get("card") == "YELLOW"]
    home_reds     = [b for b in bookings if _team_id(b) == home_id and b.get("card") in ("RED", "YELLOW_RED")]
    away_reds     = [b for b in bookings if _team_id(b) == away_id and b.get("card") in ("RED", "YELLOW_RED")]
    home_subs     = [s for s in subs     if _team_id(s) == home_id]
    away_subs     = [s for s in subs     if _team_id(s) == away_id]

    home_lineup = next((l for l in lineups if (l.get("team") or {}).get("id") == home_id), {})
    away_lineup = next((l for l in lineups if (l.get("team") or {}).get("id") == away_id), {})

    return {
        "detail":        detail,
        "goals":         goals,
        "bookings":      bookings,
        "substitutions": subs,
        "lineups":       lineups,
        "home_goals":    home_goals,
        "away_goals":    away_goals,
        "home_yellows":  home_yellows,
        "away_yellows":  away_yellows,
        "home_reds":     home_reds,
        "away_reds":     away_reds,
        "home_subs":     home_subs,
        "away_subs":     away_subs,
        "home_lineup":   home_lineup,
        "away_lineup":   away_lineup,
    }


# ── Tournament stats ───────────────────────────────────────────────────────────

async def get_tournament_stats() -> dict:
    """
    Compile WC 2026 tournament-wide stats.
    """
    today = datetime.now(timezone.utc)

    scorers_data, standings_data, finished_matches = await asyncio.gather(
        _get(f"/competitions/{WC_CODE}/scorers", {"limit": 5}),
        _get(f"/competitions/{WC_CODE}/standings"),
        get_competition_matches("2026-06-01", today.strftime("%Y-%m-%d"), status="FINISHED"),
    )

    top_scorers = (scorers_data or {}).get("scorers", [])

    total_goals      = 0
    most_goals_match = None
    most_goals_count = 0

    for match in finished_matches:
        h, a = get_score(match, "fullTime")
        if h is not None and a is not None:
            mg = h + a
            total_goals += mg
            if mg > most_goals_count:
                most_goals_count = mg
                most_goals_match = match

    played         = len(finished_matches)
    goals_per_game = round(total_goals / played, 2) if played else 0.0

    clean_sheet_leaders: list[dict] = []
    if standings_data:
        for table in (standings_data.get("standings") or []):
            for row in table.get("table", []):
                mp = row.get("playedGames", 0)
                ga = row.get("goalsAgainst", 0)
                if mp > 0:
                    clean_sheet_leaders.append({
                        "team":   row.get("team", {}),
                        "ga":     ga,
                        "played": mp,
                        "gf":     row.get("goalsFor", 0),
                        "points": row.get("points", 0),
                    })
    clean_sheet_leaders.sort(key=lambda x: (x["ga"], -x["points"]))

    return {
        "top_scorers":            top_scorers,
        "total_goals":            total_goals,
        "total_matches_played":   played,
        "goals_per_game":         goals_per_game,
        "most_goals_match":       most_goals_match,
        "most_goals_match_count": most_goals_count,
        "clean_sheet_leaders":    clean_sheet_leaders[:5],
    }
