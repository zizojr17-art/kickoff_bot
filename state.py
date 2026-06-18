"""
state.py — JSON-backed persistent state for the World Cup 2026 bot.
All mutations go through the public API; call save() after writes.
"""

import asyncio
import copy
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("state")

STATE_FILE = os.environ.get("BOT_STATE_FILE", "bot_state.json")

_state: dict[str, Any] = {}
_write_lock = threading.Lock()
_async_write_lock: asyncio.Lock | None = None
_pending_save_tasks: set[asyncio.Task] = set()


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


def _write_state(snapshot: dict[str, Any]) -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with _write_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.error("[STATE] Failed to save: %s", e)


async def _save_async(snapshot: dict[str, Any]) -> None:
    global _async_write_lock
    if _async_write_lock is None:
        _async_write_lock = asyncio.Lock()
    async with _async_write_lock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_state, snapshot)


def save() -> None:
    """Persist state without blocking the Discord event loop.

    Synchronous callers still write immediately. When called from async bot code,
    a point-in-time copy is written in the background through the default
    executor, preserving the existing JSON format while avoiding event-loop
    stalls during bursts of match events.
    """
    snapshot = copy.deepcopy(_state)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _write_state(snapshot)
        return

    task = loop.create_task(_save_async(snapshot))
    _pending_save_tasks.add(task)
    task.add_done_callback(_pending_save_tasks.discard)


async def flush_pending_saves() -> None:
    """Wait for background writes; useful in tests and graceful shutdown."""
    while _pending_save_tasks:
        await asyncio.gather(*list(_pending_save_tasks), return_exceptions=True)


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


def get_announced_subs(match_id: str) -> set:
    return set(_state.setdefault("announced_subs", {}).get(match_id, []))


def announce_sub(match_id: str, key: str) -> None:
    bucket = _state.setdefault("announced_subs", {}).setdefault(match_id, [])
    if key not in bucket:
        bucket.append(key)
    save()


def get_snapshot(match_id: str) -> dict:
    return _state.setdefault("snapshots", {}).get(match_id, {})


def set_snapshot(match_id: str, snap: dict) -> None:
    _state.setdefault("snapshots", {})[match_id] = snap
    save()


def clear_match_state(match_id: str) -> None:
    """Wipe all per-match state — used by test commands and manual cleanup."""
    for bucket in (
        "sent_events", "announced_goals", "announced_cards", "announced_subs",
        "snapshots", "score_predictions", "prediction_locks",
        "prediction_polls", "motm_votes", "motm_messages",
        "reminders_sent", "motm_results",
    ):
        _state.setdefault(bucket, {}).pop(match_id, None)
        # reminders_sent uses compound keys like "match_id:60"
        if bucket == "reminders_sent":
            for minutes in (15, 60, 90):
                _state["reminders_sent"].pop(f"{match_id}:{minutes}", None)
        # motm_results / motm_messages use "gid:match_id" keys — clean those too
        if bucket in ("motm_results", "motm_messages", "motm_votes"):
            to_del = [k for k in _state.get(bucket, {}) if k.endswith(f":{match_id}")]
            for k in to_del:
                _state[bucket].pop(k, None)
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


def mark_prediction_locked(match_id: str) -> None:
    """Mark this match's prediction window as closed (no new predictions accepted)."""
    _state.setdefault("prediction_locks", {})[match_id] = True
    save()


def is_prediction_locked(match_id: str) -> bool:
    """Return True once predictions have been locked for this match."""
    return bool(_state.get("prediction_locks", {}).get(match_id))


def lock_predictions(match_id: str) -> None:
    """Mark all existing predictions as locked AND close the prediction window."""
    preds = _state.setdefault("score_predictions", {}).get(match_id, {})
    for uid in preds:
        preds[uid]["locked"] = True
    mark_prediction_locked(match_id)


def are_predictions_locked(match_id: str) -> bool:
    """Legacy helper — prefer is_prediction_locked() for the match-level check."""
    return is_prediction_locked(match_id)


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


def get_motm_result_sent(match_id: str, gid: str) -> bool:
    """Return True if the MOTM result has already been posted for this match/guild."""
    return bool(_state.setdefault("motm_results", {}).get(f"{gid}:{match_id}"))


def mark_motm_result_sent(match_id: str, gid: str) -> None:
    _state.setdefault("motm_results", {})[f"{gid}:{match_id}"] = True
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


def get_pinned_eod_summary(gid: str) -> str | None:
    return _state.setdefault("pinned_eod_summary", {}).get(gid)


def save_pinned_eod_summary(gid: str, message_id: int) -> None:
    _state.setdefault("pinned_eod_summary", {})[gid] = str(message_id)
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


def is_leaderboard_eod_sent(date_key: str) -> bool:
    """Return True if end-of-day leaderboard has already been posted for this date."""
    return date_key in _state.setdefault("leaderboard_eod_sent", {})


def mark_leaderboard_eod_sent(date_key: str) -> None:
    _state.setdefault("leaderboard_eod_sent", {})[date_key] = True
    save()


# ── Prediction poll message IDs ───────────────────────────────────────────────

def get_prediction_poll_message(match_id: str, gid: str) -> str | None:
    return _state.setdefault("prediction_polls", {}).get(f"{gid}:{match_id}")


def save_prediction_poll_message(match_id: str, gid: str, message_id: int) -> None:
    _state.setdefault("prediction_polls", {})[f"{gid}:{match_id}"] = str(message_id)
    save()


# ── Match threads ─────────────────────────────────────────────────────────────

def get_match_thread(match_id: str, gid: str) -> str | None:
    return _state.setdefault("match_threads", {}).get(f"{gid}:{match_id}")


def save_match_thread(match_id: str, gid: str, thread_id: int) -> None:
    _state.setdefault("match_threads", {})[f"{gid}:{match_id}"] = str(thread_id)
    save()


# ── Pinned standings ──────────────────────────────────────────────────────────

def get_pinned_standings(gid: str) -> str | None:
    """Legacy: returns a single pinned standings message ID (kept for back-compat)."""
    return _state.setdefault("pinned_standings", {}).get(gid)


def save_pinned_standings(gid: str, message_id: int) -> None:
    """Legacy: save a single pinned standings ID."""
    _state.setdefault("pinned_standings", {})[gid] = str(message_id)
    save()


def get_pinned_standings_ids(gid: str) -> list[str]:
    """Return all currently pinned standings message IDs for a guild."""
    return list(_state.setdefault("pinned_standings_list", {}).get(gid, []))


def save_pinned_standings_ids(gid: str, ids: list[int]) -> None:
    """Store all currently pinned standings message IDs for a guild."""
    _state.setdefault("pinned_standings_list", {})[gid] = [str(i) for i in ids]
    save()
