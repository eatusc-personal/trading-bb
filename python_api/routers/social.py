"""Social sentiment and signal-lab endpoints."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request as FastAPIRequest

from ..db import _social_db_conn, _social_db_lock
from ..market_data import fetch_history, validate_symbol
from ..security import enforce_social_api_key
from ..social_refresh import (
    SOCIAL_RESULT_REFRESH_AFTER_SEC,
    _social_result_lock,
    _social_result_state,
    ensure_social_refresh,
)
from ..utils import now_iso, to_float

router = APIRouter()


@router.get("/api/social/sentiment")
def social_sentiment(
    request: FastAPIRequest,
    limit: int = Query(default=30, ge=5, le=100),
    experiment_id: str | None = Query(default=None, max_length=80),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    enforce_social_api_key(request)
    exp = (experiment_id or "").strip() or None

    with _social_result_lock:
        payload = _social_result_state["payload"]
        completed_at = float(_social_result_state["completed_at"] or 0.0)
        running = bool(_social_result_state["running"])
        last_error = _social_result_state["last_error"]
        started_at = float(_social_result_state["started_at"] or 0.0)

    age_sec = int(time.time() - completed_at) if completed_at else None
    stale = age_sec is None or age_sec > max(60, SOCIAL_RESULT_REFRESH_AFTER_SEC)
    if refresh or stale:
        started = ensure_social_refresh(exp)
        running = running or started

    cache_meta = {
        "age_sec": age_sec,
        "refreshing": running,
        "refresh_elapsed_sec": int(time.time() - started_at) if running and started_at else None,
        "stale": stale,
        "refresh_after_sec": max(60, SOCIAL_RESULT_REFRESH_AFTER_SEC),
        "last_error": last_error,
    }

    if payload is None:
        return {
            "updated_at": now_iso(),
            "status": "warming",
            "count": 0,
            "rows": [],
            "source_counts": {},
            "source_health": {},
            "errors": ({"scan": str(last_error)} if last_error else {}),
            "notes": {"status": "First scan is running in the background — results appear here automatically."},
            "cache": cache_meta,
        }

    out = dict(payload)
    rows = list(payload.get("rows") or [])
    out["rows"] = rows[: int(limit)]
    out["count"] = len(out["rows"])
    out["cache"] = cache_meta
    return out


@router.get("/api/social/signal-lab/runs")
def social_signal_lab_runs(request: FastAPIRequest, limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    enforce_social_api_key(request)
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            rows = conn.execute(
                """
                SELECT run_id, created_at, model_id, experiment_id, market_regime, row_count
                FROM social_signal_run
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()
    out = [
        {
            "run_id": str(r[0] or ""),
            "created_at": str(r[1] or ""),
            "model_id": str(r[2] or ""),
            "experiment_id": (str(r[3]) if r[3] is not None else None),
            "market_regime": str(r[4] or ""),
            "row_count": int(r[5] or 0),
        }
        for r in rows
    ]
    return {"updated_at": now_iso(), "count": len(out), "rows": out}


@router.get("/api/social/signal-lab/run/{run_id}")
def social_signal_lab_run(request: FastAPIRequest, run_id: str, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    enforce_social_api_key(request)
    rid = str(run_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="run_id required")

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            run_row = conn.execute(
                """
                SELECT run_id, created_at, model_id, experiment_id, market_regime, row_count
                FROM social_signal_run
                WHERE run_id = ?
                """,
                (rid,),
            ).fetchone()
            if not run_row:
                raise HTTPException(status_code=404, detail=f"run_id not found: {rid}")
            row_rows = conn.execute(
                """
                SELECT rank_position, ticker, rank_score, payload_json, created_at
                FROM social_signal_row
                WHERE run_id = ?
                ORDER BY rank_position ASC
                LIMIT ?
                """,
                (rid, int(limit)),
            ).fetchall()
        finally:
            conn.close()

    rows: list[dict[str, Any]] = []
    for rr in row_rows:
        payload_json = str(rr[3] or "")
        payload: dict[str, Any]
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except Exception:
            payload = {}
        rows.append(
            {
                "rank_position": int(rr[0] or 0),
                "ticker": str(rr[1] or ""),
                "rank_score": float(rr[2] or 0.0),
                "created_at": str(rr[4] or ""),
                "payload": payload,
            }
        )

    return {
        "updated_at": now_iso(),
        "run": {
            "run_id": str(run_row[0] or ""),
            "created_at": str(run_row[1] or ""),
            "model_id": str(run_row[2] or ""),
            "experiment_id": (str(run_row[3]) if run_row[3] is not None else None),
            "market_regime": str(run_row[4] or ""),
            "row_count": int(run_row[5] or 0),
        },
        "count": len(rows),
        "rows": rows,
    }


@router.get("/api/social/signal-lab/event-study/{run_id}")
def social_signal_lab_event_study(
    request: FastAPIRequest,
    run_id: str,
    horizons: str = Query(default="1,3,5"),
    benchmark: str = Query(default="SPY"),
    top: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    enforce_social_api_key(request)
    rid = str(run_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="run_id required")

    try:
        horizon_days = sorted(
            {
                int(x.strip())
                for x in horizons.split(",")
                if x.strip() and int(x.strip()) > 0 and int(x.strip()) <= 30
            }
        )
    except Exception:
        horizon_days = [1, 3, 5]
    if not horizon_days:
        horizon_days = [1, 3, 5]

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            run_row = conn.execute(
                """
                SELECT run_id, created_at, model_id, experiment_id, market_regime, row_count
                FROM social_signal_run
                WHERE run_id = ?
                """,
                (rid,),
            ).fetchone()
            if not run_row:
                raise HTTPException(status_code=404, detail=f"run_id not found: {rid}")
            row_rows = conn.execute(
                """
                SELECT rank_position, ticker
                FROM social_signal_row
                WHERE run_id = ?
                ORDER BY rank_position ASC
                LIMIT ?
                """,
                (rid, int(top)),
            ).fetchall()
        finally:
            conn.close()

    run_created_at = str(run_row[1] or "")
    try:
        run_dt = datetime.fromisoformat(run_created_at.replace("Z", "+00:00"))
    except Exception:
        run_dt = datetime.now(UTC)
    run_date = run_dt.date()

    def forward_return_pct(symbol: str, horizon_day: int) -> float | None:
        try:
            hist = fetch_history(symbol, "1Y", ttl=180)
        except Exception:
            return None
        if hist.empty or "close" not in hist.columns:
            return None

        dates = [pd.Timestamp(idx).date() for idx in hist.index]
        closes = [to_float(v) for v in hist["close"].tolist()]
        entry_idx = None
        for i, d in enumerate(dates):
            if d >= run_date and closes[i] is not None and float(closes[i]) > 0:
                entry_idx = i
                break
        if entry_idx is None:
            return None
        target_date = run_date + timedelta(days=int(horizon_day))
        exit_idx = None
        for i, d in enumerate(dates):
            if d >= target_date and closes[i] is not None and float(closes[i]) > 0:
                exit_idx = i
                break
        if exit_idx is None:
            return None
        entry = float(closes[entry_idx] or 0.0)
        exit_ = float(closes[exit_idx] or 0.0)
        if entry <= 0:
            return None
        return ((exit_ / entry) - 1.0) * 100.0

    benchmark_sym = validate_symbol(benchmark)
    benchmark_by_h: dict[int, float | None] = {h: forward_return_pct(benchmark_sym, h) for h in horizon_days}

    rows: list[dict[str, Any]] = []
    by_h_returns: dict[int, list[float]] = {h: [] for h in horizon_days}
    by_h_hits: dict[int, int] = {h: 0 for h in horizon_days}
    by_h_alpha: dict[int, list[float]] = {h: [] for h in horizon_days}

    for rank_position, ticker_raw in row_rows:
        ticker = str(ticker_raw or "").upper().strip()
        if not ticker:
            continue
        per_h: dict[str, Any] = {}
        valid_any = False
        for h in horizon_days:
            ret = forward_return_pct(ticker, h)
            bmk = benchmark_by_h.get(h)
            alpha = (ret - bmk) if (ret is not None and bmk is not None) else None
            per_h[f"ret_{h}d_pct"] = round(ret, 4) if ret is not None else None
            per_h[f"alpha_{h}d_pct"] = round(alpha, 4) if alpha is not None else None
            if ret is not None:
                valid_any = True
                by_h_returns[h].append(float(ret))
                if ret > 0:
                    by_h_hits[h] += 1
                if alpha is not None:
                    by_h_alpha[h].append(float(alpha))
        if valid_any:
            rows.append(
                {
                    "rank_position": int(rank_position or 0),
                    "ticker": ticker,
                    **per_h,
                }
            )

    summary: dict[str, Any] = {}
    for h in horizon_days:
        ret_vals = by_h_returns.get(h) or []
        alpha_vals = by_h_alpha.get(h) or []
        n = len(ret_vals)
        hit_rate = (by_h_hits.get(h, 0) / n) if n > 0 else None
        avg_ret = (sum(ret_vals) / n) if n > 0 else None
        avg_alpha = (sum(alpha_vals) / len(alpha_vals)) if alpha_vals else None
        summary[f"{h}d"] = {
            "samples": n,
            "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "avg_return_pct": round(avg_ret, 4) if avg_ret is not None else None,
            "benchmark_return_pct": round(benchmark_by_h.get(h), 4) if benchmark_by_h.get(h) is not None else None,
            "avg_alpha_pct": round(avg_alpha, 4) if avg_alpha is not None else None,
        }

    return {
        "updated_at": now_iso(),
        "run": {
            "run_id": str(run_row[0] or ""),
            "created_at": run_created_at,
            "model_id": str(run_row[2] or ""),
            "experiment_id": (str(run_row[3]) if run_row[3] is not None else None),
            "market_regime": str(run_row[4] or ""),
            "row_count": int(run_row[5] or 0),
        },
        "benchmark": benchmark_sym,
        "horizons_days": horizon_days,
        "count": len(rows),
        "rows": rows,
        "summary": summary,
    }
