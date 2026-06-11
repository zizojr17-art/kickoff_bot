"""
state.py — JSON-backed persistent state for the World Cup 2026 bot.
All mutations go through the public API; call save() after writes.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("state")

STATE_FILE = os.environ.get("BOT_STATE_FILE", "bot_state.json")

_state: dict[str, Any] = {}


# ── Persistence ───────────────────────────────────────────────────────────────

def load() -> None:
    global _state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                _state = json.load(f)
            log.info("[STATE] Loaded from %s (%d top-level keys)", STATE_FILE, len(_state))
        except (json.JSONDecodeError, OSError) as e:
            log.error("[STATE] Failed to load — starting fresh: %s", e)
            _state = {}
    else:
        _state = {}


def save() -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.error("[STATE] Failed to save: %s", e)


# ── Guild config ──────────────────────────────────────────────────────────────

def get_guild_config(gid: str) -> dict:
    return _state.setdefault("guilds", {}).setdefault(gid, {})


def set_guild_config(gid: str, cfg: dict) -> None:
    _state.setdefault("guilds", {})[gid] = cfg
    save()


def set_channel_id(gid: str, key: str, channel_id: str) -> None:
    cfg = get_guild_config(gid)
    cfg[key] = channel_id
    set_guild_config(gid, cfg)


def all_guild_configs() -> dict[str, dict]:
    return _state.get("guilds", {})


# ── Per-user notification mode (guild-scoped) ─────────────────────────────────

def set_user_mode(gid: str, uid: str, mode: str) -> None:
    _state.setdefault("user_modes", {}).setdefault(gid, {})[uid] = mode
    save()


def get_user_mode(gid: str, uid: str) -> str | None:
    return _state.get("user_modes", {}).get(gid, {}).get(uid)


# ── Per-user notification preferences ────────────────────────────────────────

def get_user_prefs(uid: str, gid: str) -> dict:
    return _state.setdefault("user_prefs", {}).setdefault(f"{gid}:{uid}", {})


def set_user_prefs(uid: str, gid: str, prefs: dict) -> None:
    _state.setdefault("user_prefs", {})[f"{gid}:{uid}"] = prefs
    save()


# ── Per-user team following ───────────────────────────────────────────────────

def get_user_following(uid: str) -> dict:
    return _state.setdefault("user_following", {}).setdefault(uid, {"teams": []})


def set_user_following(uid: str, data: dict) -> None:
    _state.setdefault("user_following", {})[uid] = data
    save()


# ── Match event tracking ──────────────────────────────────────────────────────

def get_sent(match_id: str) -> set:
    return set(_state.setdefault("sent_events", {}).get(match_id, []))


def mark_sent(match_id: str, event: str) -> None:
    bucket = _state.setdefault("sent_events", {}).setdefault(match_id, [])
    if event not in bucket:
        bucket.append(event)
    save()


def get_announced_goals(match_id: str) -> set:
    return set(_state.setdefault("announced_goals", {}).get(match_id, []))


def announce_goal(match_id: str, key: str) -> None:
    bucket = _state.setdefault("announced_goals", {}).setdefault(match_id, [])
    if key not in bucket:
        bucket.append(key)
    save()


def get_announced_cards(match_id: str) -> set:
    return set(_state.setdefault("announced_cards", {}).get(match_id, []))


def announce_card(match_id: str, key: str) -> None:
    bucket = _state.setdefault("announced_cards", {}).setdefault(match_id, [])
    if key not in bucket:
        bucket.append(key)
    save()


def get_snapshot(match_id: str) -> dict:
    return _state.setdefault("snapshots", {}).get(match_id, {})


def set_snapshot(match_id: str, snap: dict) -> None:
    _state.setdefault("snapshots", {})[match_id] = snap
    save()


# ── Reminders ─────────────────────────────────────────────────────────────────

def is_reminder_sent(match_id: str, minutes: int) -> bool:
    key = f"{match_id}:{minutes}"
    return key in _state.setdefault("reminders_sent", {})


def mark_reminder_sent(match_id: str, minutes: int) -> None:
    _state.setdefault("reminders_sent", {})[f"{match_id}:{minutes}"] = True
    save()


# ── Score predictions ─────────────────────────────────────────────────────────

def get_score_predictions(match_id: str) -> dict:
    return _state.setdefault("score_predictions", {}).get(match_id, {})


def save_score_prediction(uid: str, match_id: str, home: int, away: int) -> None:
    preds = _state.setdefault("score_predictions", {}).setdefault(match_id, {})
    preds[uid] = {"home": home, "away": away, "timestamp": datetime.now(timezone.utc).isoformat()}
    save()


def lock_predictions(match_id: str) -> None:
    preds = _state.setdefault("score_predictions", {}).get(match_id, {})
    for uid in preds:
        preds[uid]["locked"] = True
    save()


def are_predictions_locked(match_id: str) -> bool:
    preds = _state.setdefault("score_predictions", {}).get(match_id, {})
    if not preds:
        return False
    return all(p.get("locked", False) for p in preds.values())


# ── MOTM ──────────────────────────────────────────────────────────────────────

def get_motm_votes(match_id: str, gid: str) -> dict:
    return _state.setdefault("motm_votes", {}).get(f"{gid}:{match_id}", {})


def save_motm_votes(match_id: str, gid: str, votes: dict) -> None:
    _state.setdefault("motm_votes", {})[f"{gid}:{match_id}"] = votes
    save()


def get_motm_message_id(match_id: str, gid: str) -> str | None:
    return _state.setdefault("motm_messages", {}).get(f"{gid}:{match_id}")


def save_motm_message_id(match_id: str, gid: str, message_id: int) -> None:
    _state.setdefault("motm_messages", {})[f"{gid}:{match_id}"] = str(message_id)
    save()


# ── Leaderboard ───────────────────────────────────────────────────────────────

def get_leaderboard(gid: str) -> dict:
    return _state.setdefault("leaderboard", {}).get(gid, {})


def update_leaderboard(gid: str, uid: str, points: int) -> None:
    lb = _state.setdefault("leaderboard", {}).setdefault(gid, {})
    if uid not in lb:
        lb[uid] = {"points": 0, "exact": 0, "correct": 0, "streak": 0,
                   "best_streak": 0, "monthly": {}, "total_predictions": 0}
    entry = lb[uid]
    entry["points"]            = entry.get("points", 0) + points
    entry["total_predictions"] = entry.get("total_predictions", 0) + 1

    if points == 3:
        entry["exact"] = entry.get("exact", 0) + 1

    if points > 0:
        entry["correct"] = entry.get("correct", 0) + 1
        entry["streak"]  = entry.get("streak", 0) + 1
        best = entry.get("best_streak", 0)
        if entry["streak"] > best:
            entry["best_streak"] = entry["streak"]
    else:
        entry["streak"] = 0

    # Monthly points — key = "YYYY-MM"
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    monthly   = entry.setdefault("monthly", {})
    monthly[month_key] = monthly.get(month_key, 0) + points

    save()


def reset_leaderboard(gid: str) -> None:
    _state.setdefault("leaderboard", {})[gid] = {}
    save()


def get_monthly_leaderboard(gid: str, month_key: str | None = None) -> list[tuple[str, int]]:
    """Return [(uid, monthly_points)] sorted descending for the given month."""
    if month_key is None:
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    lb = _state.get("leaderboard", {}).get(gid, {})
    results = []
    for uid, entry in lb.items():
        pts = entry.get("monthly", {}).get(month_key, 0)
        if pts > 0:
            results.append((uid, pts))
    return sorted(results, key=lambda x: x[1], reverse=True)


def get_prediction_stats(uid: str, gid: str) -> dict:
    lb    = _state.get("leaderboard", {}).get(gid, {})
    entry = lb.get(uid, {})
    total = entry.get("total_predictions", 0)
    exact = entry.get("exact", 0)
    correct = entry.get("correct", 0)
    accuracy = round((correct / total * 100), 1) if total > 0 else 0.0
    return {
        "points":         entry.get("points", 0),
        "total":          total,
        "exact":          exact,
        "correct":        correct,
        "wrong":          total - correct,
        "accuracy":       accuracy,
        "streak":         entry.get("streak", 0),
        "best_streak":    entry.get("best_streak", 0),
        "monthly":        entry.get("monthly", {}),
    }


# ── Pinned messages ───────────────────────────────────────────────────────────

def get_pinned_leaderboard(gid: str) -> str | None:
    return _state.setdefault("pinned_leaderboard", {}).get(gid)


def save_pinned_leaderboard(gid: str, message_id: int) -> None:
    _state.setdefault("pinned_leaderboard", {})[gid] = str(message_id)
    save()


def get_pinned_table_message(gid: str) -> str | None:
    return _state.setdefault("pinned_tables", {}).get(gid)


def save_pinned_table_message(gid: str, message_id: int) -> None:
    _state.setdefault("pinned_tables", {})[gid] = str(message_id)
    save()


def get_commands_menu_message(gid: str) -> str | None:
    return _state.setdefault("commands_menu", {}).get(gid)


def save_commands_menu_message(gid: str, message_id: int) -> None:
    _state.setdefault("commands_menu", {})[gid] = str(message_id)
    save()


def get_interactive_panel_msg(gid: str, cid: str) -> str | None:
    return _state.setdefault("interactive_panels", {}).get(f"{gid}:{cid}")


def save_interactive_panel_msg(gid: str, cid: str, message_id: int) -> None:
    _state.setdefault("interactive_panels", {})[f"{gid}:{cid}"] = str(message_id)
    save()


# ── Daily summary ─────────────────────────────────────────────────────────────

def is_daily_summary_sent(date_key: str) -> bool:
    return date_key in _state.setdefault("daily_summaries_sent", {})


def mark_daily_summary_sent(date_key: str) -> None:
    _state.setdefault("daily_summaries_sent", {})[date_key] = True
    save()


# ── Prediction poll message IDs ───────────────────────────────────────────────

def get_prediction_poll_message(match_id: str, gid: str) -> str | None:
    return _state.setdefault("prediction_polls", {}).get(f"{gid}:{match_id}")


def save_prediction_poll_message(match_id: str, gid: str, message_id: int) -> None:
    _state.setdefault("prediction_polls", {})[f"{gid}:{match_id}"] = str(message_id)
    save()
