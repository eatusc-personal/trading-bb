"""Background refresh state for the social scanner (single-flight worker plus
the last completed result, seeded from SQLite on import)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any

from .db import _social_db_conn, _social_db_lock
from .social_scan import run_social_sentiment_scan

logger = logging.getLogger("openbb_api")


# ---------------------------------------------------------------------------
# Stale-while-revalidate serving for the social scanner.
#
# The live scan above takes 30-60s (paced Reddit + Stocktwits + ApeWisdom),
# which is longer than any sane browser/proxy timeout — serving it inline is
# why the Social tab used to time out. Instead the endpoint always answers
# instantly from the last completed scan and refreshes in the background:
#   - result older than SOCIAL_RESULT_REFRESH_AFTER_SEC (default 10 min) or
#     ?refresh=true  ->  single-flight background rescan
#   - process restart  ->  last persisted signal run is restored from SQLite
# ---------------------------------------------------------------------------
SOCIAL_RESULT_REFRESH_AFTER_SEC = int(os.getenv("SOCIAL_RESULT_REFRESH_AFTER_SEC", "600"))
_social_result_lock = threading.Lock()
_social_result_state: dict[str, Any] = {
    "payload": None,
    "completed_at": 0.0,
    "running": False,
    "started_at": 0.0,
    "last_error": None,
}


def _social_seed_from_db() -> None:
    """Restore the last persisted signal run so restarts don't serve an empty tab."""
    try:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                run = conn.execute(
                    "SELECT run_id, created_at FROM social_signal_run ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if not run:
                    return
                rows = conn.execute(
                    "SELECT payload_json FROM social_signal_row WHERE run_id = ? ORDER BY rank_position ASC",
                    (str(run[0]),),
                ).fetchall()
            finally:
                conn.close()
        parsed: list[dict[str, Any]] = []
        for (payload_json,) in rows:
            try:
                parsed.append(json.loads(payload_json))
            except Exception:
                continue
        if not parsed:
            return
        created_at = str(run[1] or "")
        try:
            completed_ts = datetime.fromisoformat(created_at).timestamp()
        except Exception:
            completed_ts = 0.0
        payload = {
            "updated_at": created_at,
            "count": len(parsed),
            "rows": parsed,
            "source_counts": {},
            "source_health": {},
            "errors": {},
            "notes": {"restored": "Restored from the last persisted scan after a restart; background refresh will update it."},
        }
        with _social_result_lock:
            if _social_result_state["payload"] is None:
                _social_result_state["payload"] = payload
                _social_result_state["completed_at"] = completed_ts
        logger.info("Social sentiment seeded from persisted run %s (%d rows)", run[0], len(parsed))
    except Exception as exc:
        logger.warning("Social sentiment seed-from-db failed: %s", exc)


def _social_refresh_worker(experiment_id: str | None) -> None:
    try:
        payload = run_social_sentiment_scan(limit=100, experiment_id=experiment_id)
        with _social_result_lock:
            _social_result_state["payload"] = payload
            _social_result_state["completed_at"] = time.time()
            _social_result_state["last_error"] = None
    except Exception as exc:
        logger.warning("Social sentiment background refresh failed: %s", exc)
        with _social_result_lock:
            _social_result_state["last_error"] = str(exc)
    finally:
        with _social_result_lock:
            _social_result_state["running"] = False


def ensure_social_refresh(experiment_id: str | None = None) -> bool:
    """Start a background refresh unless one is already running. Returns True if started."""
    with _social_result_lock:
        if _social_result_state["running"]:
            return False
        _social_result_state["running"] = True
        _social_result_state["started_at"] = time.time()
    thread = threading.Thread(target=_social_refresh_worker, args=(experiment_id,), daemon=True)
    thread.start()
    return True


_social_seed_from_db()
