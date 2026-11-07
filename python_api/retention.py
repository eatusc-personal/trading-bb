"""SQLite retention: the daily pruning pass over cache tables that are not
pruned at write time."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from .db import _social_db_conn, _social_db_lock

logger = logging.getLogger("openbb_api")


def prune_sqlite_caches() -> dict[str, int]:
    """Retention pass over cache tables that are not pruned at write time.

    social_baseline (45d), social_signal_run/row (14d), and symbol_master_snapshot
    (540d) are already pruned where they are written.
    """
    now = datetime.now(UTC)
    validity_cutoff = (now - timedelta(days=180)).date().isoformat()
    liveness_cutoff_minute = int((now - timedelta(days=3)).timestamp() // 60)
    after_hours_cutoff = (now - timedelta(days=30)).isoformat()
    etf_cutoff = (now - timedelta(days=60)).date().isoformat()

    deleted: dict[str, int] = {}
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            cur = conn.execute("DELETE FROM symbol_validity_cache WHERE as_of_date < ?", (validity_cutoff,))
            deleted["symbol_validity_cache"] = cur.rowcount
            cur = conn.execute("DELETE FROM quote_liveness_cache WHERE bucket_minute < ?", (liveness_cutoff_minute,))
            deleted["quote_liveness_cache"] = cur.rowcount
            cur = conn.execute("DELETE FROM after_hours_snapshot WHERE snapshot_at < ?", (after_hours_cutoff,))
            deleted["after_hours_snapshot"] = cur.rowcount
            cur = conn.execute("DELETE FROM popular_etf_snapshot WHERE snapshot_date < ?", (etf_cutoff,))
            deleted["popular_etf_snapshot"] = cur.rowcount
            conn.commit()
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()
    total = sum(max(0, n) for n in deleted.values())
    if total:
        logger.info("SQLite retention pass removed %d rows: %s", total, deleted)
    return deleted


def _schedule_daily_prune() -> None:
    def run() -> None:
        try:
            prune_sqlite_caches()
        except Exception as exc:
            logger.warning("SQLite retention pass failed: %s", exc)
        finally:
            _schedule_daily_prune()

    timer = threading.Timer(24 * 3600, run)
    timer.daemon = True
    timer.start()
