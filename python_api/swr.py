"""Generic stale-while-revalidate serving with SQLite persistence."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from .db import _social_db_conn, _social_db_lock

logger = logging.getLogger("openbb_api")


# ---------------------------------------------------------------------------
# Generic stale-while-revalidate serving.
#
# Slow read endpoints (discovery scans, RSS pulls, per-symbol history sweeps)
# should never block a request: swr_serve() answers instantly from the last
# completed result and refreshes in a single-flight background thread when the
# result is older than refresh_after_sec. Results persist to SQLite so dev
# reloads / restarts don't cause a cold start. The only time a request waits
# is the very first computation ever for a key (nothing to serve yet).
# ---------------------------------------------------------------------------
_swr_lock = threading.Lock()
_swr_states: dict[str, dict[str, Any]] = {}


def _swr_persist(key: str, payload: dict[str, Any], completed_at: float) -> None:
    try:
        blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    except Exception as exc:
        logger.warning("SWR persist skipped for %s (not JSON-serializable): %s", key, exc)
        return
    try:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO swr_cache (key, payload_json, completed_at) VALUES (?, ?, ?)",
                    (key, blob, completed_at),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        logger.warning("SWR persist failed for %s: %s", key, exc)


def _swr_load_persisted(key: str) -> tuple[dict[str, Any], float] | None:
    try:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                row = conn.execute(
                    "SELECT payload_json, completed_at FROM swr_cache WHERE key = ?", (key,)
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        return json.loads(row[0]), float(row[1])
    except Exception:
        return None


def swr_serve(
    key: str,
    producer: Any,
    refresh_after_sec: int,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (payload, cache_meta), refreshing via `producer()` in the background."""
    now = time.time()
    with _swr_lock:
        state = _swr_states.setdefault(
            key,
            {"payload": None, "completed_at": 0.0, "running": False, "last_error": None, "loaded": False},
        )
        needs_load = not state["loaded"]
        state["loaded"] = True

    if needs_load:
        persisted = _swr_load_persisted(key)
        if persisted:
            with _swr_lock:
                state = _swr_states[key]
                if state["payload"] is None:
                    state["payload"], state["completed_at"] = persisted

    with _swr_lock:
        state = _swr_states[key]
        payload = state["payload"]
        completed_at = float(state["completed_at"] or 0.0)
        running = bool(state["running"])
        last_error = state["last_error"]

    if payload is None:
        # First-ever computation for this key: run inline so the caller gets data.
        result = producer()
        done = time.time()
        with _swr_lock:
            state = _swr_states[key]
            state["payload"] = result
            state["completed_at"] = done
            state["last_error"] = None
        _swr_persist(key, result, done)
        return result, {"age_sec": 0, "refreshing": False, "stale": False, "refresh_after_sec": refresh_after_sec, "last_error": None}

    age_sec = int(now - completed_at) if completed_at else None
    stale = age_sec is None or age_sec > max(15, refresh_after_sec)

    if (stale or force_refresh) and not running:
        def _worker() -> None:
            try:
                result = producer()
                done = time.time()
                with _swr_lock:
                    st = _swr_states[key]
                    st["payload"] = result
                    st["completed_at"] = done
                    st["last_error"] = None
                _swr_persist(key, result, done)
            except Exception as exc:
                logger.warning("SWR background refresh failed for %s: %s", key, exc)
                with _swr_lock:
                    _swr_states[key]["last_error"] = str(exc)
            finally:
                with _swr_lock:
                    _swr_states[key]["running"] = False

        with _swr_lock:
            state = _swr_states[key]
            if not state["running"]:
                state["running"] = True
                threading.Thread(target=_worker, daemon=True, name=f"swr:{key}").start()
        running = True

    meta = {
        "age_sec": age_sec,
        "refreshing": running,
        "stale": stale,
        "refresh_after_sec": refresh_after_sec,
        "last_error": last_error,
    }
    return payload, meta
