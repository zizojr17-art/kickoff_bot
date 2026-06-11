"""
Async wrapper around football-data.org v4 API.
- Exponential backoff for 429 / 5xx / timeouts
- Single-request bulk fetching; individual detail only when needed
- [API] log prefix for easy filtering
"""
import aiohttp
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("bot.api")

BASE_URL = "https://api.football-data.org/v4"
HEADERS  = {"X-Auth-Token": os.environ["FOOTBALL_API_KEY"]}

# Semaphore keeps burst requests below the free-tier 10 req/min limit.
_sem = asyncio.Semaphore(6)

# Simple in-memory team cache to avoid repeated competition fetches.
_team_cache: dict[str, dict] = {}   # lower-case name → team dict

# WC 2026 match-number index (populated once on startup).
# Maps str(match_id) → 1-based sequential match number sorted by utcDate.
_wc_match_order: dict[str, int] = {}
_wc_match_order_loaded: bool = False

COMPETITION_CODES: dict[str, str] = {
    "WC":  "FIFA World Cup",
    "CL":  "UEFA Champions League",
    "PL":  "Premier League",
    "BL1": "Bundesliga",
    "SA":  "Serie A",
    "PD":  "La Liga",
    "FL1": "Ligue 1",
    "EC":  "UEFA European Championship",
    "CLI": "CONMEBOL Libertadores",
    "PPL": "Primeira Liga",
    "DED": "Eredivisie",
}

STAGE_NAMES: dict[str, str] = {
    "GROUP_STAGE":           "Group Stage",
    "LAST_16":               "Round of 16",
    "QUARTER_FINALS":        "Quarter-finals",
    "SEMI_FINALS":           "Semi-finals",
    "FINAL":                 "Final",
    "THIRD_PLACE":           "Third Place",
    "PLAY_OFF_ROUND":        "Play-off",
    "PRELIMINARY_ROUND":     "Preliminary Round",
    "1ST_QUALIFYING_ROUND":  "1st Qualifying Round",
    "2ND_QUALIFYING_ROUND":  "2nd Qualifying Round",
    "3RD_QUALIFYING_ROUND":  "3rd Qualifying Round",
}


# ── Core fetch with exponential backoff ───────────────────────────────────────

async def fetch(path: str, *, max_retries: int = 3) -> Optional[dict]:
    url = f"{BASE_URL}{path}"
    for attempt in range(max_retries):
        async with _sem:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, headers=HEADERS,
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:

                        if resp.status == 200:
                            data = await resp.json()
                            return data

                        if resp.status == 429:
                            wait = min(60 * (2 ** attempt), 900)
                            log.warning("[API] Rate-limited on %s — backing off %ds", path, wait)
                            await asyncio.sleep(wait)
                            continue

                        if resp.status >= 500:
                            wait = min(10 * (2 ** attempt), 120)
                            log.warning("[API] Server error %s on %s — retry in %ds", resp.status, path, wait)
                            await asyncio.sleep(wait)
                            continue

                        body = await resp.text()
                        log.warning("[API] HTTP %s for %s: %s", resp.status, path, body[:200])
                        return None

            except asyncio.TimeoutError:
                wait = min(5 * (2 ** attempt), 60)
                log.warning("[API] Timeout on %s (attempt %d/%d) — retry in %ds", path, attempt + 1, max_retries, wait)
                await asyncio.sleep(wait)

            except aiohttp.ClientError as e:
                log.error("[API] Client error on %s: %s", path, e)
                return None

    log.error("[API] All %d retries exhausted for %s", max_retries, path)
    return None


# ── Match endpoints ───────────────────────────────────────────────────────────

async def get_todays_matches() -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = await fetch(f"/matches?dateFrom={today}&dateTo={today}")
    return data.get("matches", []) if data else []


async def get_match_detail(match_id: int | str) -> Optional[dict]:
    """Fetch full match detail (includes goals, bookings, stats)."""
    return await fetch(f"/matches/{match_id}")


async def get_live_matches() -> list[dict]:
    data = await fetch("/matches?status=IN_PLAY,PAUSED,EXTRA_TIME,PENALTY_SHOOTOUT")
    return data.get("matches", []) if data else []


async def get_standings(competition: str) -> Optional[dict]:
    return await fetch(f"/competitions/{competition}/standings")


async def get_competition_matches(competition: str, date_from: str, date_to: str) -> list[dict]:
    data = await fetch(f"/competitions/{competition}/matches?dateFrom={date_from}&dateTo={date_to}")
    return data.get("matches", []) if data else []


async def load_wc_match_order() -> None:
    """Fetch all WC 2026 matches once and build a match-number index sorted by date."""
    global _wc_match_order, _wc_match_order_loaded
    data = await fetch("/competitions/WC/matches?season=2026")
    if data:
        ms = sorted(data.get("matches", []), key=lambda m: m.get("utcDate", ""))
        _wc_match_order = {str(m["id"]): i + 1 for i, m in enumerate(ms)}
        log.info("[API] WC 2026 match order loaded: %d matches indexed", len(_wc_match_order))
    else:
        log.warning("[API] Could not load WC 2026 match order (tournament may not be available yet)")
    _wc_match_order_loaded = True


def wc_match_number(match_id: int | str) -> Optional[int]:
    """Return the 1-based WC 2026 match number, or None if unknown."""
    return _wc_match_order.get(str(match_id))


async def get_competition_teams(competition: str) -> list[dict]:
    data = await fetch(f"/competitions/{competition}/teams")
    return (data.get("teams") or []) if data else []


async def get_team(team_id: int | str) -> Optional[dict]:
    return await fetch(f"/teams/{team_id}")


async def get_team_matches(team_id: int | str, *, status: str = "FINISHED", limit: int = 5) -> list[dict]:
    data = await fetch(f"/teams/{team_id}/matches?status={status}&limit={limit}")
    return (data.get("matches") or []) if data else []


async def search_team(name: str) -> Optional[dict]:
    """
    Find a team by fuzzy name match across major competitions.
    Returns the first match found, or None. Uses a simple in-memory cache.
    """
    needle = name.lower().strip()

    # Check cache first
    if needle in _team_cache:
        return _team_cache[needle]

    # Search key competitions (WC first — most commonly searched)
    for code in ("WC", "CL", "PL", "BL1", "SA", "PD", "FL1"):
        teams = await get_competition_teams(code)
        for t in teams:
            for field in ("name", "shortName", "tla"):
                val = (t.get(field) or "").lower()
                if needle in val or val in needle:
                    _team_cache[needle] = t
                    # Also cache by the team's own name for future hits
                    _team_cache[(t.get("shortName") or t.get("name") or "").lower()] = t
                    return t
    return None


async def get_next_match(competition: Optional[str] = None) -> Optional[dict]:
    """Return the earliest upcoming match (optionally filtered by competition)."""
    now   = datetime.now(timezone.utc)
    limit = now + timedelta(days=3)
    df    = now.strftime("%Y-%m-%d")
    dt    = limit.strftime("%Y-%m-%d")

    if competition:
        matches = await get_competition_matches(competition.upper(), df, dt)
    else:
        data = await fetch(f"/matches?dateFrom={df}&dateTo={dt}&status=SCHEDULED,TIMED")
        matches = data.get("matches", []) if data else []

    scheduled = [m for m in matches if m.get("status") in ("SCHEDULED", "TIMED")]
    if not scheduled:
        return None
    return min(scheduled, key=lambda m: m.get("utcDate", ""))


# ── Pure-Python helpers ───────────────────────────────────────────────────────

def parse_dt(utc_str: str) -> Optional[datetime]:
    if not utc_str:
        return None
    try:
        return datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def flag_emoji(country_code: str) -> str:
    """ISO-2 country code → Unicode flag emoji."""
    if not country_code or len(country_code) < 2:
        return ""
    try:
        cc = country_code[:2].upper()
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc)
    except Exception:
        return ""


def team_display(team: dict) -> str:
    name = team.get("shortName") or team.get("name") or "Unknown"
    cc   = (team.get("area") or {}).get("countryCode", "")
    flag = flag_emoji(cc)
    return f"{flag} {name}".strip() if flag else name


def get_score(match: dict, phase: str = "fullTime") -> tuple[int | None, int | None]:
    block = (match.get("score") or {}).get(phase) or {}
    return block.get("home"), block.get("away")


def get_current_score(match: dict) -> tuple[int | None, int | None]:
    h, a = get_score(match, "fullTime")
    if h is None:
        h, a = get_score(match, "halfTime")
    return h, a


def get_stage(match: dict) -> str:
    raw = match.get("stage") or match.get("group") or ""
    return STAGE_NAMES.get(raw, raw.replace("_", " ").title())


def get_venue(match: dict) -> str:
    return match.get("venue") or ""


def penalty_scores(match: dict) -> tuple[int | None, int | None]:
    return get_score(match, "penalties")


def goal_key(goal: dict) -> str:
    """Stable unique key for a goal event."""
    minute   = goal.get("minute", "?")
    scorer   = goal.get("scorer") or {}
    s_id     = scorer.get("id") or scorer.get("name") or "unknown"
    g_type   = goal.get("type", "REGULAR")
    return f"{minute}_{s_id}_{g_type}"


def card_key(booking: dict) -> str:
    """Stable unique key for a booking event."""
    minute   = booking.get("minute", "?")
    player   = booking.get("player") or {}
    p_id     = player.get("id") or player.get("name") or "unknown"
    card     = booking.get("card", "RED")
    return f"{minute}_{p_id}_{card}"
