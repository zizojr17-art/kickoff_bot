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


# ── Reminder flags (60m / 15m / kickoff / HT / FT …) ─────────────────────────

def get_sent(match_id) -> set:
    return set(_state.get("reminders_sent", {}).get(str(match_id), []))


def mark_sent(match_id, *keys: str) -> None:
    bucket = _state.setdefault("reminders_sent", {})
    existing = set(bucket.get(str(match_id), []))
    existing.update(keys)
    bucket[str(match_id)] = list(existing)
    _save()


# ── Event-level tracking (goals & red cards) ──────────────────────────────────

def get_announced_goals(match_id) -> set:
    return set(_state.get("match_events", {}).get(str(match_id), {}).get("goals", []))


def announce_goal(match_id, key: str) -> None:
    ev = _state.setdefault("match_events", {}).setdefault(str(match_id), {})
    goals = set(ev.get("goals", []))
    goals.add(key)
    ev["goals"] = list(goals)
    _save()
    log.info("[GOAL] Persisted goal key %s for match %s", key, match_id)


def get_announced_cards(match_id) -> set:
    return set(_state.get("match_events", {}).get(str(match_id), {}).get("red_cards", []))


def announce_card(match_id, key: str) -> None:
    ev = _state.setdefault("match_events", {}).setdefault(str(match_id), {})
    cards = set(ev.get("red_cards", []))
    cards.add(key)
    ev["red_cards"] = list(cards)
    _save()
    log.info("[CARD] Persisted card key %s for match %s", key, match_id)


# ── Match snapshots ───────────────────────────────────────────────────────────

def get_snapshot(match_id) -> dict:
    return _state.get("snapshots", {}).get(str(match_id), {})


def set_snapshot(match_id, snap: dict) -> None:
    _state.setdefault("snapshots", {})[str(match_id)] = snap
    _save()


# ── Guild configuration ───────────────────────────────────────────────────────

def get_guild_config(guild_id) -> dict:
    return _state.get("guild_config", {}).get(str(guild_id), {})


def set_guild_config(guild_id, data: dict) -> None:
    _state.setdefault("guild_config", {})[str(guild_id)] = data
    _save()


def all_guild_configs() -> dict:
    return _state.get("guild_config", {})


def get_channel_id(guild_id, key: str):
    """Generic getter for any named channel ID in guild config."""
    return get_guild_config(str(guild_id)).get(key)


def set_channel_id(guild_id, key: str, channel_id: str) -> None:
    """Generic setter for any named channel ID in guild config."""
    cfg = get_guild_config(str(guild_id))
    cfg[key] = str(channel_id)
    set_guild_config(str(guild_id), cfg)


# ── Daily summary ─────────────────────────────────────────────────────────────

def is_daily_summary_sent(key: str) -> bool:
    return key in _state.get("daily_summary_sent", [])


def mark_daily_summary_sent(key: str) -> None:
    bucket = _state.setdefault("daily_summary_sent", [])
    if key not in bucket:
        bucket.append(key)
        _state["daily_summary_sent"] = bucket[-60:]
    _save()


# ── Score predictions ─────────────────────────────────────────────────────────

def save_score_prediction(user_id, match_id, home_score: int, away_score: int) -> None:
    """Save a user's exact-score prediction."""
    preds = _state.setdefault("score_predictions", {})
    preds.setdefault(str(match_id), {})[str(user_id)] = {
        "home": int(home_score),
        "away": int(away_score),
    }
    _save()


def get_score_predictions(match_id) -> dict:
    """Returns {user_id_str: {home: int, away: int}}"""
    return _state.get("score_predictions", {}).get(str(match_id), {})


# ── Poll / prediction message IDs ─────────────────────────────────────────────

def save_poll_message_id(match_id, guild_id, message_id) -> None:
    _state.setdefault("poll_messages", {}).setdefault(str(match_id), {})[str(guild_id)] = str(message_id)
    _save()


def get_poll_message_id(match_id, guild_id):
    return _state.get("poll_messages", {}).get(str(match_id), {}).get(str(guild_id))


# ── MOTM messages & votes ─────────────────────────────────────────────────────

def save_motm_message_id(match_id, guild_id, message_id) -> None:
    _state.setdefault("motm_messages", {}).setdefault(str(match_id), {})[str(guild_id)] = str(message_id)
    _save()


def get_motm_message_id(match_id, guild_id):
    return _state.get("motm_messages", {}).get(str(match_id), {}).get(str(guild_id))


def save_motm_votes(match_id, guild_id, votes: dict) -> None:
    """Save {user_id_str: player_name} vote map for a guild."""
    _state.setdefault("motm_votes", {}).setdefault(str(match_id), {})[str(guild_id)] = votes
    _save()


def get_motm_votes(match_id, guild_id) -> dict:
    return _state.get("motm_votes", {}).get(str(match_id), {}).get(str(guild_id), {})


# ── Leaderboard ───────────────────────────────────────────────────────────────

def update_leaderboard(guild_id, user_id, points: int) -> None:
    lb = _state.setdefault("leaderboard", {}).setdefault(str(guild_id), {})
    lb[str(user_id)] = lb.get(str(user_id), 0) + points
    _save()


def get_leaderboard(guild_id) -> dict:
    """Returns {user_id_str: total_points}"""
    return _state.get("leaderboard", {}).get(str(guild_id), {})


def reset_leaderboard(guild_id) -> None:
    _state.setdefault("leaderboard", {})[str(guild_id)] = {}
    _save()


def save_pinned_leaderboard_message(guild_id, message_id) -> None:
    _state.setdefault("pinned_leaderboard", {})[str(guild_id)] = str(message_id)
    _save()


def get_pinned_leaderboard_message(guild_id):
    return _state.get("pinned_leaderboard", {}).get(str(guild_id))


# ── Pinned group tables ───────────────────────────────────────────────────────

def save_pinned_table_message(guild_id, message_id) -> None:
    _state.setdefault("pinned_tables", {})[str(guild_id)] = str(message_id)
    _save()


def get_pinned_table_message(guild_id):
    return _state.get("pinned_tables", {}).get(str(guild_id))


# ── Commands menu ─────────────────────────────────────────────────────────────

def save_commands_menu_message(guild_id, message_id) -> None:
    _state.setdefault("commands_menus", {})[str(guild_id)] = str(message_id)
    _save()


def get_commands_menu_message(guild_id):
    return _state.get("commands_menus", {}).get(str(guild_id))


# ── Per-user notification mode ────────────────────────────────────────────────

def set_user_mode(guild_id, user_id, mode: str) -> None:
    _state.setdefault("user_modes", {}).setdefault(str(guild_id), {})[str(user_id)] = mode
    _save()


def get_user_mode(guild_id, user_id) -> str:
    return _state.get("user_modes", {}).get(str(guild_id), {}).get(str(user_id), "standard")
