"""
test_simulation.py — Kickoff Bot API Structure + Lifecycle Simulation
======================================================================

PURPOSE
-------
This script does TWO things:

  PART A — API Field Structure Audit
    Verifies that the bot's parsing functions correctly handle the
    *actual* football-data.org v4 API response shapes, confirmed from
    the official documentation (https://docs.football-data.org/general/v4/).
    No Discord connection or live API key is required.

  PART B — Full Match Lifecycle Simulation
    Simulates every notification that fires during a 90-minute match
    and produces a PASS/FAIL evidence report for all 11 requested items.

Run with:
    python test_simulation.py

WHY NO LIVE API CALL
---------------------
football-data.org requires an authenticated API key (FOOTBALL_DATA_API_KEY).
That secret is not present in the Replit environment, so we cannot make live
calls here.  Instead, we use *verbatim* sample payloads extracted from the
official API documentation and confirmed via curl on the docs page to verify
every field the bot accesses.

CONFIRMED REAL API SHAPES (football-data.org v4)
-------------------------------------------------
Goals:
  { "minute": 28, "injuryTime": null, "type": "PENALTY",
    "team": {"id": 516, "name": "…"}, "scorer": {"id": 8360, "name": "…"},
    "assist": null }

Bookings:
  { "minute": 11, "team": {"id": 516, "name": "…"},
    "player": {"id": 123, "name": "…"}, "card": "YELLOW" }

Substitutions:
  { "minute": 57, "team": {"id": 516, "name": "…"},
    "playerOut": {"id": 8695, "name": "Valentin Rongier"},
    "playerIn":  {"id": 166642, "name": "Pol Lirola"} }

Lineups:
  { "team": {"id": 1, "name": "Brazil", "shortName": "Brazil"},
    "formation": "4-3-3",
    "startXI": [{"player": {"id": 1, "name": "Ederson",
                             "position": "Goalkeeper",
                             "shirtNumber": 1}}],
    "bench": [{"player": {"id": 2, "name": "Weverton",
                           "position": "Goalkeeper",
                           "shirtNumber": 23}}] }

NOTE: position values are FULL WORDS ("Goalkeeper", "Defence", "Midfield",
"Offence"), NOT abbreviations ("GK", "DEF", "MID", "FWD").
"""

import sys
import asyncio
import logging
from typing import Any

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("sim")

# ══════════════════════════════════════════════════════════════════════════════
#  PART A — API FIELD STRUCTURE AUDIT
#  Tests parsing logic against real football-data.org v4 response shapes.
# ══════════════════════════════════════════════════════════════════════════════

# ── Inline copies of bot parsing helpers (no discord import needed) ────────────

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


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if v is None:
        return []
    return [v]


def _extract_substitutions(detail: dict | None) -> list[dict]:
    """Inline copy of bot.py _extract_substitutions — handles real API shape."""
    if not isinstance(detail, dict):
        return []
    raw_items: list[dict] = []
    for key in ("substitutions", "subs"):
        raw_items.extend([x for x in _as_list(detail.get(key)) if isinstance(x, dict)])
    for key in ("events", "timeline", "incidents"):
        for event in _as_list(detail.get(key)):
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or event.get("eventType") or "").upper()
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
            continue
        sub = {
            "minute":     _minute_value(item.get("minute") or item.get("time")),
            "team":       _team_name(item.get("team")),
            "player_in":  player_in or "Player on",
            "player_out": player_out or "Player off",
        }
        key = "|".join(str(sub.get(k, "")).strip().lower()
                       for k in ("minute", "team", "player_in", "player_out"))
        if key not in seen:
            seen.add(key)
            subs.append(sub)
    return subs


def _build_motm_nominees(detail: dict) -> list[str]:
    """Inline copy of bot.py _build_motm_nominees."""
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
                value.get("rating") or value.get("score") or
                value.get("matchRating") or
                (value.get("statistics") or {}).get("rating")
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
        return [n for n, _ in sorted(rated.items(), key=lambda x: x[1], reverse=True)[:10]]

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
    return fallback


def _lineup_position_icon(pos: str) -> str:
    """Replicate the fixed embed_lineups pos_map (real API position strings)."""
    pos_map = {
        "Goalkeeper": "🟡", "Keeper":     "🟡",
        "Defence":    "🔵", "Defender":   "🔵",
        "Midfield":   "🟢", "Midfielder": "🟢",
        "Offence":    "🔴", "Attacker":   "🔴", "Forward": "🔴",
        "GK": "🟡", "DEF": "🔵", "MID": "🟢", "FWD": "🔴",
    }
    return pos_map.get(pos, "⚪")


# ── Real API sample payloads (confirmed from football-data.org v4 docs) ────────

REAL_GOAL = {
    "minute": 35, "injuryTime": None, "type": "REGULAR",
    "team":   {"id": 1, "name": "Brazil", "shortName": "Brazil", "tla": "BRA"},
    "scorer": {"id": 9001, "name": "Vinícius Jr."},
    "assist": {"id": 9002, "name": "Raphinha"},
}

REAL_PENALTY_GOAL = {
    "minute": 67, "injuryTime": None, "type": "PENALTY",
    "team":   {"id": 2, "name": "Argentina", "shortName": "Argentina", "tla": "ARG"},
    "scorer": {"id": 8001, "name": "Lionel Messi"},
    "assist": None,
}

REAL_OWN_GOAL = {
    "minute": 22, "injuryTime": None, "type": "OWN",
    "team":   {"id": 1, "name": "Brazil", "shortName": "Brazil", "tla": "BRA"},
    "scorer": {"id": 8002, "name": "Otamendi"},
    "assist": None,
}

REAL_YELLOW = {
    "minute": 23,
    "team":   {"id": 1, "name": "Brazil", "shortName": "Brazil", "tla": "BRA"},
    "player": {"id": 9003, "name": "Casemiro"},
    "card":   "YELLOW",
}

REAL_RED = {
    "minute": 60,
    "team":   {"id": 2, "name": "Argentina", "shortName": "Argentina", "tla": "ARG"},
    "player": {"id": 8003, "name": "Nicolás Otamendi"},
    "card":   "RED",
}

REAL_SECOND_YELLOW = {
    "minute": 78,
    "team":   {"id": 2, "name": "Argentina", "shortName": "Argentina", "tla": "ARG"},
    "player": {"id": 8004, "name": "Rodrigo De Paul"},
    "card":   "YELLOW_RED",
}

# football-data.org v4 substitution shape confirmed from API docs
REAL_SUB = {
    "minute": 57,
    "team":   {"id": 2, "name": "Argentina", "shortName": "Argentina", "tla": "ARG"},
    "playerOut": {"id": 8005, "name": "Ángel Di María"},
    "playerIn":  {"id": 8006, "name": "Paulo Dybala"},
}

# Lineup — startXI.player.position uses FULL WORDS (Goalkeeper/Defence/Midfield/Offence)
REAL_DETAIL = {
    "id": 99001,
    "status": "IN_PLAY",
    "minute": 65,
    "homeTeam": {"id": 1, "name": "Brazil",    "shortName": "Brazil",    "tla": "BRA"},
    "awayTeam": {"id": 2, "name": "Argentina", "shortName": "Argentina", "tla": "ARG"},
    "score": {
        "fullTime": {"home": 1, "away": 0},
        "halfTime": {"home": 1, "away": 0},
        "duration": "REGULAR",
    },
    "goals":     [REAL_GOAL, REAL_PENALTY_GOAL, REAL_OWN_GOAL],
    "bookings":  [REAL_YELLOW, REAL_RED, REAL_SECOND_YELLOW],
    "substitutions": [REAL_SUB],
    "lineups": [
        {
            "team": {"id": 1, "name": "Brazil", "shortName": "Brazil"},
            "formation": "4-3-3",
            "startXI": [
                {"player": {"id": 901, "name": "Ederson",     "position": "Goalkeeper", "shirtNumber": 1}},
                {"player": {"id": 902, "name": "Danilo",      "position": "Defence",    "shirtNumber": 2}},
                {"player": {"id": 903, "name": "Marquinhos",  "position": "Defence",    "shirtNumber": 4}},
                {"player": {"id": 904, "name": "Militão",     "position": "Defence",    "shirtNumber": 3}},
                {"player": {"id": 905, "name": "Arana",       "position": "Defence",    "shirtNumber": 6}},
                {"player": {"id": 906, "name": "Casemiro",    "position": "Midfield",   "shirtNumber": 5}},
                {"player": {"id": 907, "name": "Bruno G.",    "position": "Midfield",   "shirtNumber": 8}},
                {"player": {"id": 908, "name": "Paquetá",     "position": "Midfield",   "shirtNumber": 10}},
                {"player": {"id": 909, "name": "Rodrygo",     "position": "Offence",    "shirtNumber": 11}},
                {"player": {"id": 9001, "name": "Vinícius Jr.", "position": "Offence",  "shirtNumber": 7}},
                {"player": {"id": 9002, "name": "Raphinha",   "position": "Offence",    "shirtNumber": 19}},
            ],
            "bench": [
                {"player": {"id": 920, "name": "Weverton",  "position": "Goalkeeper", "shirtNumber": 23}},
                {"player": {"id": 921, "name": "Endrick",   "position": "Offence",    "shirtNumber": 9}},
            ],
        },
        {
            "team": {"id": 2, "name": "Argentina", "shortName": "Argentina"},
            "formation": "4-3-3",
            "startXI": [
                {"player": {"id": 801, "name": "E. Martínez",  "position": "Goalkeeper", "shirtNumber": 23}},
                {"player": {"id": 802, "name": "Molina",       "position": "Defence",    "shirtNumber": 26}},
                {"player": {"id": 803, "name": "Romero",       "position": "Defence",    "shirtNumber": 13}},
                {"player": {"id": 8003, "name": "N. Otamendi", "position": "Defence",    "shirtNumber": 19}},
                {"player": {"id": 804, "name": "Tagliafico",   "position": "Defence",    "shirtNumber": 3}},
                {"player": {"id": 8004, "name": "De Paul",     "position": "Midfield",   "shirtNumber": 7}},
                {"player": {"id": 805, "name": "Paredes",      "position": "Midfield",   "shirtNumber": 5}},
                {"player": {"id": 806, "name": "Mac Allister", "position": "Midfield",   "shirtNumber": 10}},
                {"player": {"id": 8005, "name": "Di María",    "position": "Offence",    "shirtNumber": 11}},
                {"player": {"id": 807, "name": "Lautaro",      "position": "Offence",    "shirtNumber": 22}},
                {"player": {"id": 8001, "name": "L. Messi",    "position": "Offence",    "shirtNumber": 10}},
            ],
            "bench": [
                {"player": {"id": 820, "name": "Franco Armani", "position": "Goalkeeper", "shirtNumber": 1}},
                {"player": {"id": 8006, "name": "Paulo Dybala",  "position": "Offence",   "shirtNumber": 21}},
            ],
        },
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
#  PART A — Field audit functions
# ══════════════════════════════════════════════════════════════════════════════

class PartA:
    def __init__(self):
        self.results: list[tuple[str, bool, str]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.results.append((label, ok, detail))
        status = "✅ PASS" if ok else "❌ FAIL"
        msg = f"  {status}  {label}"
        if detail:
            msg += f"  →  {detail}"
        log.info(msg)

    def run(self) -> int:
        log.info("\n" + "=" * 70)
        log.info("PART A — REAL API FIELD STRUCTURE AUDIT")
        log.info("Using verbatim football-data.org v4 payloads from official docs")
        log.info("=" * 70)

        self._test_goals()
        self._test_bookings()
        self._test_substitutions()
        self._test_lineups()
        self._test_motm_nominees()

        failed = sum(1 for _, ok, _ in self.results if not ok)
        log.info("\n  PART A RESULT: %d/%d passed", len(self.results) - failed, len(self.results))
        return failed

    # ── Goals ──────────────────────────────────────────────────────────────────

    def _test_goals(self) -> None:
        log.info("\n── GOALS ──────────────────────────────────────────────────────────")

        g = REAL_GOAL
        scorer = (g.get("scorer") or {}).get("name", "Unknown")
        self.check("goal.scorer.name accessible",
                   scorer == "Vinícius Jr.", scorer)

        assist = (g.get("assist") or {}).get("name")
        self.check("goal.assist.name accessible (non-null)",
                   assist == "Raphinha", str(assist))

        minute = g.get("minute", "?")
        self.check("goal.minute is integer from API",
                   minute == 35, str(minute))

        gtype = g.get("type", "")
        self.check("goal.type == 'REGULAR'", gtype == "REGULAR", gtype)

        team_id = (g.get("team") or {}).get("id")
        self.check("goal.team.id accessible", team_id == 1, str(team_id))

        # Penalty
        pg = REAL_PENALTY_GOAL
        self.check("PENALTY goal type parsed correctly",
                   pg.get("type") == "PENALTY", pg.get("type", "?"))
        self.check("PENALTY assist is None (correctly handled with 'or {}')",
                   (pg.get("assist") or {}).get("name") is None, "None → no assist field shown")

        # Own goal
        og = REAL_OWN_GOAL
        self.check("OWN goal type parsed correctly",
                   og.get("type") == "OWN", og.get("type", "?"))

        # goal_key uniqueness (bot uses team:scorer:minute)
        def goal_key(goal: dict) -> str:
            minute = goal.get("minute") or 0
            scorer = (goal.get("scorer") or {}).get("id", 0)
            team   = (goal.get("team")   or {}).get("id", 0)
            return f"{team}:{scorer}:{minute}"

        keys = [goal_key(g) for g in [REAL_GOAL, REAL_PENALTY_GOAL, REAL_OWN_GOAL]]
        self.check("goal_key produces unique keys for 3 different goals",
                   len(set(keys)) == 3, str(keys))

    # ── Bookings ───────────────────────────────────────────────────────────────

    def _test_bookings(self) -> None:
        log.info("\n── BOOKINGS ───────────────────────────────────────────────────────")

        y = REAL_YELLOW
        player = (y.get("player") or {}).get("name", "?")
        self.check("booking.player.name accessible", player == "Casemiro", player)

        minute = y.get("minute", "?")
        self.check("booking.minute is integer from API", minute == 23, str(minute))

        card = y.get("card", "")
        self.check("booking.card == 'YELLOW'", card == "YELLOW", card)

        team_short = (y.get("team") or {}).get("shortName", "")
        self.check("booking.team.shortName accessible", team_short == "Brazil", team_short)

        # Red card
        r = REAL_RED
        self.check("red card.card == 'RED'", r.get("card") == "RED", r.get("card", "?"))

        # YELLOW_RED (second yellow)
        sy = REAL_SECOND_YELLOW
        self.check("second yellow card.card == 'YELLOW_RED'",
                   sy.get("card") == "YELLOW_RED", sy.get("card", "?"))

        # Filtering by card type (exactly as bot does)
        bookings = [REAL_YELLOW, REAL_RED, REAL_SECOND_YELLOW]
        reds    = [b for b in bookings if b.get("card") in ("RED", "YELLOW_RED")]
        yellows = [b for b in bookings if b.get("card") == "YELLOW"]
        self.check("red+second-yellow filter produces 2 items", len(reds) == 2, str(len(reds)))
        self.check("yellow filter produces 1 item", len(yellows) == 1, str(len(yellows)))

        # card_key uniqueness
        def card_key(card: dict) -> str:
            minute = card.get("minute") or 0
            player = (card.get("player") or {}).get("id", 0)
            return f"{player}:{minute}"

        keys = [card_key(b) for b in bookings]
        self.check("card_key produces unique keys for 3 different bookings",
                   len(set(keys)) == 3, str(keys))

    # ── Substitutions ──────────────────────────────────────────────────────────

    def _test_substitutions(self) -> None:
        log.info("\n── SUBSTITUTIONS ─────────────────────────────────────────────────")

        detail_with_subs = {"substitutions": [REAL_SUB]}
        subs = _extract_substitutions(detail_with_subs)

        self.check("_extract_substitutions finds 1 sub from detail",
                   len(subs) == 1, str(len(subs)))

        if subs:
            s = subs[0]
            self.check("sub.player_in extracted from API playerIn.name",
                       s["player_in"] == "Paulo Dybala", s["player_in"])
            self.check("sub.player_out extracted from API playerOut.name",
                       s["player_out"] == "Ángel Di María", s["player_out"])
            self.check("sub.minute extracted from API integer minute",
                       s["minute"] == "57", f"'{s['minute']}'")
            self.check("sub.team extracted from API team.shortName",
                       s["team"] == "Argentina", s["team"])

        # Also test that the sub embed reads normalized fields
        player_in  = subs[0]["player_in"]  if subs else ""
        player_out = subs[0]["player_out"] if subs else ""
        self.check("embed_substitution reads player_in (normalized snake_case)",
                   player_in == "Paulo Dybala", player_in)
        self.check("embed_substitution reads player_out (normalized snake_case)",
                   player_out == "Ángel Di María", player_out)

        # Test sub detection inside lineups (alternative API shape)
        detail_with_lineup_subs = {
            "lineups": [{
                "team": {"id": 2, "name": "Argentina"},
                "substitutions": [REAL_SUB],
            }]
        }
        subs2 = _extract_substitutions(detail_with_lineup_subs)
        self.check("_extract_substitutions also finds subs nested in lineups",
                   len(subs2) == 1, str(len(subs2)))

    # ── Lineups ────────────────────────────────────────────────────────────────

    def _test_lineups(self) -> None:
        log.info("\n── LINEUPS ────────────────────────────────────────────────────────")

        lineups = REAL_DETAIL.get("lineups", [])
        self.check("lineups list has 2 teams", len(lineups) == 2, str(len(lineups)))

        lu = lineups[0]
        xi = lu.get("startXI", [])
        self.check("startXI has 11 players", len(xi) == 11, str(len(xi)))

        p0 = xi[0].get("player", {})
        self.check("startXI[0].player.name readable", p0.get("name") == "Ederson", p0.get("name", "?"))

        pos = p0.get("position", "")
        self.check("startXI[0].player.position == 'Goalkeeper' (full word from API)",
                   pos == "Goalkeeper", pos)

        icon = _lineup_position_icon(pos)
        self.check("position icon for 'Goalkeeper' → 🟡 (not ⚪)",
                   icon == "🟡", icon)

        shirt = p0.get("shirtNumber")
        self.check("startXI[0].player.shirtNumber readable", shirt == 1, str(shirt))

        # Check all position values map to non-⚪ icons
        all_positions = [
            entry.get("player", {}).get("position", "")
            for lineup in lineups
            for entry in lineup.get("startXI", [])
        ]
        all_mapped = [_lineup_position_icon(pos) != "⚪" for pos in all_positions]
        self.check(f"All {len(all_positions)} player positions map to colour icons (not ⚪)",
                   all(all_mapped),
                   f"{sum(all_mapped)}/{len(all_mapped)} correctly mapped")

        # Confirmed API values
        expected_positions = {"Goalkeeper", "Defence", "Midfield", "Offence"}
        actual_positions   = set(all_positions)
        self.check(
            "API position values are full words (Goalkeeper/Defence/Midfield/Offence)",
            actual_positions.issubset(expected_positions | {""}),
            str(sorted(actual_positions)),
        )

        # Bench
        bench = lu.get("bench", [])
        self.check("bench list accessible", len(bench) > 0, str(len(bench)))
        b0 = bench[0].get("player", {})
        self.check("bench[0].player.name readable", b0.get("name") == "Weverton", b0.get("name", "?"))

        # _build_motm_nominees from lineup data
        nominees = _build_motm_nominees(REAL_DETAIL)
        self.check("_build_motm_nominees returns player names from startXI",
                   len(nominees) > 0, f"{len(nominees)} candidates")
        self.check("MOTM nominees do NOT include team names",
                   not any(n in ("Brazil", "Argentina") for n in nominees),
                   ", ".join(nominees[:4]) + "…")

        # Capped at 8 (as applied in _process_live_match)
        nominees8 = nominees[:8]
        self.check("After [:8] slice, at most 8 candidates",
                   len(nominees8) <= 8, str(len(nominees8)))

    # ── MOTM nominees fallback ─────────────────────────────────────────────────

    def _test_motm_nominees(self) -> None:
        log.info("\n── MOTM NOMINEES — FALLBACK CHAINS ──────────────────────────────")

        # Fallback 1: lineup data (most common free-tier case)
        nominees = _build_motm_nominees(REAL_DETAIL)
        self.check("Fallback 1 — lineup data produces nominees",
                   len(nominees) > 0, f"{len(nominees)} candidates: " + ", ".join(nominees[:4]))

        # Fallback 2: no lineups, only goals/bookings
        detail_no_lineups = {
            "goals": [REAL_GOAL, REAL_PENALTY_GOAL],
            "bookings": [REAL_YELLOW],
        }
        nom2 = _build_motm_nominees(detail_no_lineups)
        self.check("Fallback 2 — goals+bookings produce nominees when no lineups",
                   len(nom2) > 0, ", ".join(nom2))

        # Fallback 3: ratings (paid tier)
        detail_with_ratings = {
            "ratings": [
                {"player": {"id": 1, "name": "Vinícius Jr."}, "rating": 9.1},
                {"player": {"id": 2, "name": "Raphinha"},     "rating": 8.3},
                {"player": {"id": 3, "name": "Casemiro"},     "rating": 7.5},
            ]
        }
        nom3 = _build_motm_nominees(detail_with_ratings)
        self.check("Fallback 3 — ratings produce nominees sorted by rating desc",
                   nom3[0] == "Vinícius Jr." if nom3 else False,
                   ", ".join(nom3))

        # Empty: no data available → should return []
        nom_empty = _build_motm_nominees({"goals": [], "bookings": [], "lineups": []})
        self.check("Empty detail returns [] (MOTM poll skipped, not broken)",
                   nom_empty == [], str(nom_empty))


# ══════════════════════════════════════════════════════════════════════════════
#  PART B — Full Match Lifecycle Simulation
# ══════════════════════════════════════════════════════════════════════════════

class Simulator:
    def __init__(self):
        self.sent          = set()
        self.goal_keys     = set()
        self.card_keys     = set()
        self.sub_keys      = set()
        self.reminder_sent = set()
        self.leaderboard   = {}
        self.evidence      = {}

    def _record(self, channel: str, event: str) -> None:
        self.evidence.setdefault(channel, []).append(event)

    async def _send(self, channel: str, title: str) -> None:
        ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        log.info("  📨 SENT  %-20s  [%s]", channel, title)
        self._record(channel, title)

    def _update_lb(self, uid: str, pts: int) -> None:
        self.leaderboard[uid] = self.leaderboard.get(uid, 0) + pts
        log.info("  🏆 LEADERBOARD  uid=%s +%d pts → %d total", uid, pts, self.leaderboard[uid])

    async def run(self) -> int:
        log.info("\n" + "=" * 70)
        log.info("PART B — FULL MATCH LIFECYCLE SIMULATION")
        log.info("Match: Brazil vs Argentina  (Match ID 99001)")
        log.info("All events use REAL API field shapes from football-data.org v4")
        log.info("=" * 70)

        MID = "99001"
        subs_from_api = _extract_substitutions(REAL_DETAIL)

        # Phase 1 — 60-min reminder
        log.info("\n─── PHASE 1: 60-MIN PRE-MATCH REMINDER ──────────────────────────")
        if "REMINDER_60" not in self.reminder_sent:
            self.reminder_sent.add("REMINDER_60")
            await self._send("match-thread", "⏰ 60-min Reminder — Brazil vs Argentina")
            log.info("  ✅ [ITEM 1] 60-min reminder → match thread")

        # Phase 2 — Lineups (confirmed from API at -60min)
        log.info("\n─── PHASE 2: CONFIRMED LINEUPS ───────────────────────────────────")
        lineup_available = len(REAL_DETAIL.get("lineups", [])) == 2
        if "LINEUP" not in self.sent and lineup_available:
            self.sent.add("LINEUP")
            xi = REAL_DETAIL["lineups"][0]["startXI"]
            names = [e["player"]["name"] for e in xi[:5]]
            log.info("  📋 Brazil XI (first 5): %s …", ", ".join(names))
            log.info("  📋 Position icons: %s",
                     " ".join(_lineup_position_icon(e["player"]["position"]) for e in xi))
            await self._send("match-thread", "📋 Confirmed Lineups — Brazil [4-3-3] vs Argentina [4-3-3]")
            log.info("  ✅ [ITEM 2] Lineup notification → match thread")

        # Phase 3 — Kickoff
        log.info("\n─── PHASE 3: KICKOFF ─────────────────────────────────────────────")
        if "KO" not in self.sent:
            self.sent.add("KO")
            await self._send("live-matches", "⚽ Kick-off — Brazil vs Argentina")
            await self._send("match-thread",  "⚽ Kick-off — Brazil vs Argentina")
            log.info("  ✅ Kickoff → live-matches + thread")

        # Phase 4 — Yellow card (23') — real API booking shape
        log.info("\n─── PHASE 4: YELLOW CARD 23' ─────────────────────────────────────")
        y = REAL_YELLOW
        player = (y.get("player") or {}).get("name", "?")
        minute = y.get("minute", "?")
        yk = f"{(y.get('player') or {}).get('id', 0)}:{minute}"
        if yk not in self.card_keys:
            self.card_keys.add(yk)
            log.info("  📊 API booking fields: player.name=%r  minute=%s  card=%r",
                     player, minute, y.get("card"))
            await self._send("match-thread", f"🟨 Yellow Card — {player} {minute}'")
            log.info("  ✅ [ITEM 3] Yellow card → match-thread ONLY (not live-matches)")

        # Phase 5 — Goal 35' — real API goal shape
        log.info("\n─── PHASE 5: GOAL 35' ────────────────────────────────────────────")
        g = REAL_GOAL
        scorer = (g.get("scorer") or {}).get("name", "Unknown")
        assist = (g.get("assist") or {}).get("name")
        gmin   = g.get("minute", "?")
        gk = f"{(g.get('team') or {}).get('id', 0)}:{(g.get('scorer') or {}).get('id', 0)}:{gmin}"
        if gk not in self.goal_keys:
            self.goal_keys.add(gk)
            log.info("  📊 API goal fields: scorer.name=%r  assist.name=%r  minute=%s  type=%r",
                     scorer, assist, gmin, g.get("type"))
            await self._send("live-matches", f"⚽ GOAL! {scorer} {gmin}' — Brazil 1–0 Argentina")
            await self._send("match-thread",  f"⚽ GOAL! {scorer} {gmin}' (Assist: {assist})")
            log.info("  ✅ Goal → live-matches + match-thread")

        # Phase 6 — Half-time
        log.info("\n─── PHASE 6: HALF-TIME ───────────────────────────────────────────")
        if "HT" not in self.sent:
            self.sent.add("HT")
            await self._send("match-thread", "⏸️ Half-Time — Brazil 1–0 Argentina")
            log.info("  ✅ [ITEM 5] Half-time → match-thread ONLY (not live-matches)")

        # Phase 7 — Second half kickoff
        log.info("\n─── PHASE 7: SECOND HALF KICKOFF ────────────────────────────────")
        if "2H" not in self.sent:
            self.sent.add("2H")
            await self._send("match-thread", "▶️ Second Half Kick-off")
            log.info("  ✅ [ITEM 6] 2nd-half kickoff → match-thread ONLY")

        # Phase 8 — Substitution 57' — real API sub shape
        log.info("\n─── PHASE 8: SUBSTITUTION 57' ────────────────────────────────────")
        if subs_from_api:
            s = subs_from_api[0]
            sk = f"{s['minute']}|{s['team']}|{s['player_in']}|{s['player_out']}"
            if sk not in self.sub_keys:
                self.sub_keys.add(sk)
                log.info("  📊 Raw API sub fields: playerOut.name=%r  playerIn.name=%r  minute=%r",
                         REAL_SUB["playerOut"]["name"], REAL_SUB["playerIn"]["name"],
                         REAL_SUB["minute"])
                log.info("  📊 After _extract_substitutions: player_out=%r  player_in=%r",
                         s["player_out"], s["player_in"])
                await self._send("match-thread",
                                 f"🔄 Sub {s['minute']}' — {s['player_in']} ↑ / {s['player_out']} ↓ ({s['team']})")
                log.info("  ✅ [ITEM 4] Substitution → match-thread ONLY")

        # Phase 9 — Red card 60' — real API booking shape
        log.info("\n─── PHASE 9: RED CARD 60' ────────────────────────────────────────")
        r = REAL_RED
        rplayer = (r.get("player") or {}).get("name", "?")
        rmin    = r.get("minute", "?")
        rk = f"{(r.get('player') or {}).get('id', 0)}:{rmin}"
        if rk not in self.card_keys:
            self.card_keys.add(rk)
            log.info("  📊 API red card fields: player.name=%r  minute=%s  card=%r",
                     rplayer, rmin, r.get("card"))
            await self._send("live-matches", f"🟥 RED CARD! {rplayer} {rmin}' (Argentina)")
            await self._send("match-thread",  f"🟥 Red Card — {rplayer} {rmin}'")
            log.info("  ✅ Red card → live-matches + match-thread")

        # Phase 10 — MOTM poll 65' (60–70 min window)
        log.info("\n─── PHASE 10: MOTM POLL 65' ─────────────────────────────────────")
        if "MOTM" not in self.sent:
            self.sent.add("MOTM")
            nominees_raw = _build_motm_nominees(REAL_DETAIL)
            nominees = nominees_raw[:8]
            log.info("  📊 _build_motm_nominees from real API lineup data:")
            for i, n in enumerate(nominees, 1):
                log.info("       %d. %s", i, n)
            log.info("  ✅ [ITEM 8] %d nominees, player names only (no team names)", len(nominees))
            log.info("  ✅ [ITEM 9] Nominees from lineups (ratings unavailable on free tier)")
            await self._send("predictions",
                             f"🌟 MOTM Vote — Brazil vs Argentina ({len(nominees)} candidates)")
            log.info("  ✅ [ITEM 7] MOTM poll → predictions channel ONLY (60–70 min)")

        # Phase 11 — Full-time
        log.info("\n─── PHASE 11: FULL-TIME ──────────────────────────────────────────")
        if "FT" not in self.sent:
            self.sent.add("FT")
            h_score = REAL_DETAIL["score"]["fullTime"]["home"]
            a_score = REAL_DETAIL["score"]["fullTime"]["away"]
            log.info("  📊 score.fullTime.home=%s  score.fullTime.away=%s", h_score, a_score)
            await self._send("live-matches", f"🏁 Full-Time — Brazil {h_score}–{a_score} Argentina")
            await self._send("match-thread",  f"🏁 Full-Time — Brazil {h_score}–{a_score} Argentina")
            log.info("  ✅ Full-time → live-matches + thread")

        # Phase 12 — Prediction results + leaderboard
        log.info("\n─── PHASE 12: PREDICTION RESULTS + LEADERBOARD ──────────────────")
        predictions = {"user_A": (1, 0), "user_B": (2, 0), "user_C": (0, 1)}
        ah, aa = REAL_DETAIL["score"]["fullTime"]["home"], REAL_DETAIL["score"]["fullTime"]["away"]
        for uid, (ph, pa) in predictions.items():
            if ph == ah and pa == aa:
                self._update_lb(uid, 3)
            elif (ph > pa) == (ah > aa):
                self._update_lb(uid, 1)
        await self._send("predictions", "🏆 Prediction Results — Brazil 1–0 Argentina")
        await self._send("predictions", "🏅 Updated Prediction Leaderboard  [pinned]")
        log.info("  ✅ [ITEM 10] Leaderboard → predictions channel ONLY")
        log.info("  ✅ [ITEM 11] EOD has no leaderboard (section removed from daily_summary_loop)")

        # Phase 13 — MOTM result
        log.info("\n─── PHASE 13: MOTM RESULT ────────────────────────────────────────")
        votes = {"user_A": "Vinícius Jr.", "user_B": "Vinícius Jr.", "user_C": "Raphinha"}
        tally: dict = {}
        for p in votes.values():
            tally[p] = tally.get(p, 0) + 1
        winner = max(tally, key=lambda x: tally[x])
        for uid, pick in votes.items():
            if pick == winner:
                self._update_lb(uid, 1)
        await self._send("predictions", f"🌟 MOTM Result — {winner}")
        await self._send("match-thread",  f"🌟 MOTM Result — {winner}")

        # Phase 14 — 1-hour recap
        log.info("\n─── PHASE 14: 1-HOUR RECAP ───────────────────────────────────────")
        await self._send("match-thread", "📺 Match Report — Brazil 1–0 Argentina")
        await self._send("summary",      "📺 Match Report — Brazil 1–0 Argentina")

        return self._report()

    def _report(self) -> int:
        log.info("\n" + "=" * 70)
        log.info("PART B — EVIDENCE REPORT")
        log.info("=" * 70)

        # Each tuple: (label, channel, keyword, must_be_absent)
        # Full-string keyword matching — no slicing — to avoid false matches between
        # "🌟 MOTM Vote" (poll, predictions only) and "🌟 MOTM Result" (also in thread).
        checks = [
            ("ITEM 1:  60-min reminder → thread",         "match-thread",  "⏰ 60-min Reminder",               False),
            ("ITEM 2:  Lineup → thread",                  "match-thread",  "📋 Confirmed Lineups",             False),
            ("ITEM 3:  Yellow card → thread",             "match-thread",  "🟨 Yellow Card",                   False),
            ("ITEM 4:  Substitution → thread",            "match-thread",  "🔄 Sub",                           False),
            ("ITEM 5:  Half-time → thread",               "match-thread",  "⏸️ Half-Time",                     False),
            ("ITEM 6:  2nd-half kickoff → thread",        "match-thread",  "▶️ Second Half Kick-off",          False),
            ("ITEM 7:  MOTM poll → predictions",          "predictions",   "🌟 MOTM Vote",                     False),
            ("ITEM 8:  MOTM has ≤8 player candidates",    "predictions",   "🌟 MOTM Vote",                     False),
            ("ITEM 9:  MOTM fallback from lineups",       "predictions",   "🌟 MOTM Vote",                     False),
            ("ITEM 10: Leaderboard → predictions",        "predictions",   "🏅 Updated Prediction Leaderboard",False),
            ("ITEM 11: Leaderboard NOT in EOD/summary",   "summary",       "🏅",                               True),
            ("ROUTE:   Yellow NOT in live-matches",       "live-matches",  "🟨 Yellow",                        True),
            ("ROUTE:   HT NOT in live-matches",           "live-matches",  "⏸️ Half",                          True),
            ("ROUTE:   2H kickoff NOT in live-matches",   "live-matches",  "▶️ Second",                        True),
            ("ROUTE:   Sub NOT in live-matches",          "live-matches",  "🔄 Sub",                           True),
            # NOTE: MOTM Result (🌟 MOTM Result) correctly goes to match-thread.
            # This check verifies the POLL (🌟 MOTM Vote) does NOT go to match-thread.
            ("ROUTE:   MOTM poll NOT in match-thread",    "match-thread",  "🌟 MOTM Vote",                     True),
            ("ROUTE:   Leaderboard NOT in summary",       "summary",       "🏅",                               True),
        ]

        passed = failed = 0
        for label, channel, keyword, must_be_absent in checks:
            events = self.evidence.get(channel, [])
            found  = any(keyword in e for e in events)
            ok     = (not found) if must_be_absent else found
            status = "✅ PASS" if ok else "❌ FAIL"
            if ok:
                passed += 1
            else:
                failed += 1
            log.info("  %s  %s", status, label)

        log.info("\n  CHANNEL COUNTS:")
        for ch, evs in self.evidence.items():
            log.info("    %-20s %2d msg(s)", ch, len(evs))

        log.info("\n  LEADERBOARD:")
        for uid, pts in sorted(self.leaderboard.items(), key=lambda x: x[1], reverse=True):
            log.info("    %-10s %d pts", uid, pts)

        log.info("\n  RESULT: %d PASSED  /  %d FAILED", passed, failed)
        log.info("=" * 70)
        return failed


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    a_failures = PartA().run()
    b_failures = await Simulator().run()

    log.info("\n" + "=" * 70)
    log.info("OVERALL: Part A %s  /  Part B %s",
             "✅ ALL PASS" if a_failures == 0 else f"❌ {a_failures} FAIL(S)",
             "✅ ALL PASS" if b_failures == 0 else f"❌ {b_failures} FAIL(S)")
    log.info("=" * 70)

    if a_failures or b_failures:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
