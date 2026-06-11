"""
Persistent state with atomic writes and event-level tracking.
All saves use a write-to-tmp + os.replace() pattern to prevent corruption.
"""
import json
import logging
import os

log = logging.getLogger("bot.state")

_DIR = os.path.dirname(__file__)
STATE_FILE = os.path.join(_DIR, "state.json")
_TMP_FILE  = os.path.join(_DIR, "state.tmp")

_state: dict = {}


# ── Load / Save ───────────────────────────────────────────────────────────────

def load() -> None:
    global _state
    try:
        with open(STATE_FILE) as f:
            _state = json.load(f)
        log.info("[STATE] Loaded from %s", STATE_FILE)
    except FileNotFoundError:
        _state = {}
        log.info("[STATE] No state file — starting fresh")
    except json.JSONDecodeError as e:
        _state = {}
        log.warning("[STATE] File corrupt (%s) — starting fresh", e)


def _save() -> None:
    """Atomic save: write to tmp then rename so a crash never corrupts state."""
    try:
        with open(_TMP_FILE, "w") as f:
            json.dump(_state, f, indent=2)
        os.replace(_TMP_FILE, STATE_FILE)
    except Exception as e:
        log.error("[STATE] Atomic save failed: %s", e)


# ── High-level reminder flags (60m / 15m / kickoff / HT / FT …) ──────────────

def get_sent(match_id: int | str) -> set[str]:
    return set(_state.get("reminders_sent", {}).get(str(match_id), []))


def mark_sent(match_id: int | str, *keys: str) -> None:
    bucket = _state.setdefault("reminders_sent", {})
    existing = set(bucket.get(str(match_id), []))
    existing.update(keys)
    bucket[str(match_id)] = list(existing)
    _save()


# ── Event-level tracking (goals & red cards by unique key) ────────────────────
#
# Goal key   : "{minute}_{scorer_id_or_name}_{type}"  e.g. "45_1234_REGULAR"
# Card key   : "{minute}_{player_id_or_name}_{card}"  e.g. "61_5678_RED"
#
# This lets the bot determine exactly which events have already been announced
# after a restart, so it never reposts a goal or card.

def get_announced_goals(match_id: int | str) -> set[str]:
    return set(_state.get("match_events", {}).get(str(match_id), {}).get("goals", []))


def announce_goal(match_id: int | str, key: str) -> None:
    me = _state.setdefault("match_events", {})
    ev = me.setdefault(str(match_id), {})
    goals = set(ev.get("goals", []))
    goals.add(key)
    ev["goals"] = list(goals)
    _save()
    log.info("[GOAL] Persisted goal key %s for match %s", key, match_id)


def get_announced_cards(match_id: int | str) -> set[str]:
    return set(_state.get("match_events", {}).get(str(match_id), {}).get("red_cards", []))


def announce_card(match_id: int | str, key: str) -> None:
    me = _state.setdefault("match_events", {})
    ev = me.setdefault(str(match_id), {})
    cards = set(ev.get("red_cards", []))
    cards.add(key)
    ev["red_cards"] = list(cards)
    _save()
    log.info("[CARD] Persisted card key %s for match %s", key, match_id)


# ── Match snapshots (score + status for change detection) ─────────────────────

def get_snapshot(match_id: int | str) -> dict:
    return _state.get("snapshots", {}).get(str(match_id), {})


def set_snapshot(match_id: int | str, snap: dict) -> None:
    _state.setdefault("snapshots", {})[str(match_id)] = snap
    _save()


# ── Guild configuration ───────────────────────────────────────────────────────

def get_guild_config(guild_id: int | str) -> dict:
    return _state.get("guild_config", {}).get(str(guild_id), {})


def set_guild_config(guild_id: int | str, data: dict) -> None:
    _state.setdefault("guild_config", {})[str(guild_id)] = data
    _save()


def all_guild_configs() -> dict[str, dict]:
    return _state.get("guild_config", {})


# ── Daily summary — keyed by "{guild_id}_{YYYY-MM-DD}" for per-TZ support ────

def is_daily_summary_sent(key: str) -> bool:
    return key in _state.get("daily_summary_sent", [])


def mark_daily_summary_sent(key: str) -> None:
    bucket = _state.setdefault("daily_summary_sent", [])
    if key not in bucket:
        bucket.append(key)
        _state["daily_summary_sent"] = bucket[-60:]  # keep ~2 months
    _save()
