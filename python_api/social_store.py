"""SQLite persistence for social data: the hourly mention baseline and the
signal-lab run history."""

from __future__ import annotations

import json
import math
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import _social_db_conn, _social_db_lock
from .utils import now_iso


def upsert_social_baseline(bucket_hour: int, rows: dict[str, dict[str, float]]) -> None:
    if not rows:
        return
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            for ticker, metrics in rows.items():
                mentions = int(metrics.get("mentions") or 0)
                unique_authors = int(metrics.get("unique_authors") or 0)
                engagement = float(metrics.get("engagement") or 0.0)
                conn.execute(
                    """
                    INSERT INTO social_baseline (ticker, bucket_hour, mentions, unique_authors, engagement)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(ticker, bucket_hour) DO UPDATE SET
                        mentions = MAX(mentions, excluded.mentions),
                        unique_authors = MAX(unique_authors, excluded.unique_authors),
                        engagement = MAX(engagement, excluded.engagement)
                    """,
                    (ticker, bucket_hour, mentions, unique_authors, engagement),
                )
            # keep ~45d
            cutoff = bucket_hour - (45 * 24)
            conn.execute("DELETE FROM social_baseline WHERE bucket_hour < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()


def social_baseline_stats(tickers: list[str], now_ts: float) -> dict[str, dict[str, float]]:
    if not tickers:
        return {}
    end_bucket = int(now_ts // 3600) - 1
    start_bucket = end_bucket - (30 * 24) + 1
    out: dict[str, dict[str, float]] = {}

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            for ticker in tickers:
                rows = conn.execute(
                    """
                    SELECT mentions, unique_authors, engagement
                    FROM social_baseline
                    WHERE ticker = ? AND bucket_hour >= ? AND bucket_hour <= ?
                    """,
                    (ticker, start_bucket, end_bucket),
                ).fetchall()
                if not rows:
                    out[ticker] = {
                        "samples": 0,
                        "avg_mentions_1h_30d": 0.0,
                        "avg_unique_authors_1h_30d": 0.0,
                        "avg_engagement_1h_30d": 0.0,
                        "std_engagement_1h_30d": 0.0,
                    }
                    continue
                mentions_arr = [float(r[0]) for r in rows]
                authors_arr = [float(r[1]) for r in rows]
                engagement_arr = [float(r[2]) for r in rows]
                avg_mentions = sum(mentions_arr) / len(mentions_arr)
                avg_authors = sum(authors_arr) / len(authors_arr)
                avg_eng = sum(engagement_arr) / len(engagement_arr)
                variance = sum((x - avg_eng) ** 2 for x in engagement_arr) / max(1, len(engagement_arr))
                std_eng = math.sqrt(variance)
                out[ticker] = {
                    "samples": float(len(rows)),
                    "avg_mentions_1h_30d": avg_mentions,
                    "avg_unique_authors_1h_30d": avg_authors,
                    "avg_engagement_1h_30d": avg_eng,
                    "std_engagement_1h_30d": std_eng,
                }
        finally:
            conn.close()
    return out


def persist_social_signal_run(
    rows: list[dict[str, Any]],
    *,
    model_id: str,
    market_regime: str,
    experiment_id: str | None = None,
) -> str:
    if not rows:
        return ""
    run_id = f"run_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    created_at = now_iso()
    sample_rows = rows[:50]

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            conn.execute(
                """
                INSERT INTO social_signal_run (run_id, created_at, model_id, experiment_id, market_regime, row_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    created_at,
                    model_id,
                    (experiment_id or None),
                    market_regime,
                    len(sample_rows),
                ),
            )
            for idx, row in enumerate(sample_rows, start=1):
                conn.execute(
                    """
                    INSERT INTO social_signal_row (run_id, rank_position, ticker, rank_score, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        idx,
                        str(row.get("ticker") or ""),
                        float(row.get("rank_score") or 0.0),
                        json.dumps(row, separators=(",", ":"), ensure_ascii=True),
                        created_at,
                    ),
                )
            # Keep the most recent 14 days of signal-lab rows.
            cutoff = (datetime.now(UTC) - timedelta(days=14)).isoformat()
            stale_runs = conn.execute(
                "SELECT run_id FROM social_signal_run WHERE created_at < ?",
                (cutoff,),
            ).fetchall()
            for rr in stale_runs:
                old_run_id = str(rr[0] or "")
                if not old_run_id:
                    continue
                conn.execute("DELETE FROM social_signal_row WHERE run_id = ?", (old_run_id,))
                conn.execute("DELETE FROM social_signal_run WHERE run_id = ?", (old_run_id,))
            conn.commit()
        finally:
            conn.close()
    return run_id
