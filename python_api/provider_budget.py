"""Per-provider request budgets so free-tier API limits are never exceeded.

Tracks two windows per provider:
- per-minute: in-memory sliding window (resets on process restart, which is safe:
  a restart can only make us more conservative within the minute).
- per-day (UTC): persisted in SQLite so uvicorn --reload restarts and dev cycles
  don't reset the daily counter.

Defaults match the documented free tiers, scaled by a safety margin so manual
curl testing and other tools sharing the key still have headroom:
- twelvedata: 8 credits/min, 800/day (1 credit per symbol on batch quotes)
- fmp:        10/min (pacing), 250/day
- marketstack: 5/min (pacing), 3/day (free tier is 100/month)
- yfinance:    120/min politeness pacing, no daily cap

Override per provider with env vars, e.g. BUDGET_TWELVEDATA_PER_DAY=800,
BUDGET_FMP_PER_MIN=5, and the global margin with PROVIDER_BUDGET_SAFETY_PCT.
A limit of 0 means unlimited for that window.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("openbb_api.budget")

BUDGET_DB = os.path.join(os.path.dirname(__file__), "provider_budget.db")

_DEFAULTS: dict[str, tuple[int, int]] = {
    # provider: (per_minute, per_day); 0 = unlimited
    "twelvedata": (8, 800),
    "fmp": (10, 250),
    "marketstack": (5, 3),
    # yfinance has no published quota: this is politeness pacing only. Keep it
    # high enough that a cold all-groups divergence pulse (~225 history fetches)
    # PLUS the chart board (up to 9) and normal tab polling in the same minute
    # never self-throttle; actual concurrency is capped by worker pools and the
    # history cache anyway. Tune with BUDGET_YFINANCE_PER_MIN (0 = unlimited).
    "yfinance": (600, 0),
}

SAFETY_PCT = max(0.1, min(1.0, float(os.getenv("PROVIDER_BUDGET_SAFETY_PCT", "0.9"))))


def _limits_for(provider: str) -> tuple[int, int]:
    default_min, default_day = _DEFAULTS.get(provider, (0, 0))
    per_min = int(os.getenv(f"BUDGET_{provider.upper()}_PER_MIN", str(default_min)))
    per_day = int(os.getenv(f"BUDGET_{provider.upper()}_PER_DAY", str(default_day)))
    if per_day > 0:
        per_day = max(1, int(per_day * SAFETY_PCT))
    return per_min, per_day


_lock = threading.Lock()
# provider -> list of (timestamp, cost) within the last minute
_minute_events: dict[str, list[tuple[float, int]]] = {}
# (provider, utc_day) -> count, mirror of the SQLite row to avoid a read per check
_day_counts: dict[tuple[str, str], int] = {}
_db_ready = False


def _utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(BUDGET_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_day_usage (
            provider TEXT NOT NULL,
            day TEXT NOT NULL,
            used INTEGER NOT NULL,
            PRIMARY KEY (provider, day)
        )
        """
    )
    return conn


def _load_day_count(provider: str, day: str) -> int:
    key = (provider, day)
    if key in _day_counts:
        return _day_counts[key]
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT used FROM provider_day_usage WHERE provider = ? AND day = ?",
            (provider, day),
        ).fetchone()
        used = int(row[0]) if row else 0
        # opportunistic cleanup of old days
        conn.execute("DELETE FROM provider_day_usage WHERE day < ?", (day,))
        conn.commit()
    finally:
        conn.close()
    _day_counts.clear()  # drop stale prior-day mirrors
    _day_counts[key] = used
    return used


def _persist_day_count(provider: str, day: str, used: int) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO provider_day_usage (provider, day, used) VALUES (?, ?, ?)
            ON CONFLICT(provider, day) DO UPDATE SET used = excluded.used
            """,
            (provider, day, used),
        )
        conn.commit()
    finally:
        conn.close()


def _minute_used(provider: str, now: float) -> int:
    events = _minute_events.get(provider, [])
    fresh = [(ts, cost) for ts, cost in events if now - ts < 60.0]
    _minute_events[provider] = fresh
    return sum(cost for _, cost in fresh)


def try_consume(provider: str, cost: int = 1) -> tuple[bool, str]:
    """Atomically check and record `cost` requests for `provider`.

    Returns (True, "") when within budget, or (False, reason) when the call
    should be skipped. Callers should treat a denial like a provider cooldown
    and fall through to the next provider.
    """
    cost = max(1, int(cost))
    per_min, per_day = _limits_for(provider)
    now = time.time()
    day = _utc_day()

    with _lock:
        if per_min > 0:
            used_min = _minute_used(provider, now)
            if used_min + cost > per_min:
                return False, f"budget: per-minute limit reached ({used_min}/{per_min})"
        if per_day > 0:
            try:
                used_day = _load_day_count(provider, day)
            except Exception as exc:
                logger.warning("Budget DB unavailable, allowing request: %s", exc)
                used_day = 0
            if used_day + cost > per_day:
                return False, f"budget: daily limit reached ({used_day}/{per_day})"

        _minute_events.setdefault(provider, []).append((now, cost))
        if per_day > 0:
            new_used = _day_counts.get((provider, day), 0) + cost
            _day_counts[(provider, day)] = new_used
            try:
                _persist_day_count(provider, day, new_used)
            except Exception as exc:
                logger.warning("Failed to persist budget usage for %s: %s", provider, exc)
    return True, ""


def snapshot() -> dict[str, Any]:
    now = time.time()
    day = _utc_day()
    out: dict[str, Any] = {"safety_pct": SAFETY_PCT, "day_utc": day, "providers": {}}
    with _lock:
        for provider in _DEFAULTS:
            per_min, per_day = _limits_for(provider)
            try:
                used_day = _load_day_count(provider, day)
            except Exception:
                used_day = -1
            out["providers"][provider] = {
                "per_minute_limit": per_min or None,
                "per_minute_used": _minute_used(provider, now),
                "per_day_limit": per_day or None,
                "per_day_used": used_day,
                "per_day_remaining": (max(0, per_day - used_day) if per_day > 0 and used_day >= 0 else None),
            }
    return out
