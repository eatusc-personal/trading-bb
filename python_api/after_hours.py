"""After-hours (pre/post market) movers: TradingView scanner fetch, SQLite
snapshot persistence and the payload builder behind /api/market/after-hours."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import AFTER_HOURS_SNAPSHOT_MAX_AGE_HOURS, MARKET_DATA_TIMEOUT_SEC
from .db import _social_db_conn, _social_db_lock
from .market_data import market_cap_bucket
from .net import fetch_json_url
from .utils import finite_or, now_iso, parse_iso_dt, to_float


def infer_after_hours_asset_group(asset_type: str) -> str:
    text = str(asset_type or "").strip().lower()
    if text in {"fund", "etf"}:
        return "etf"
    return "stocks"


def fetch_tradingview_after_hours(session_type: str, limit: int = 300) -> list[dict[str, Any]]:
    session = "postmarket" if str(session_type or "").lower().startswith("post") else "premarket"
    sort_field = f"{session}_change_abs"
    close_field = f"{session}_close"
    change_field = f"{session}_change"
    payload = {
        "range": [0, max(50, min(1000, int(limit)))],
        "sort": {"sortBy": sort_field, "sortOrder": "desc"},
        "columns": [
            "name",
            "description",
            "type",
            "exchange",
            "close",
            "volume",
            "market_cap_basic",
            close_field,
            change_field,
            sort_field,
        ],
    }
    raw = fetch_json_url(
        "https://scanner.tradingview.com/america/scan",
        timeout=max(6, MARKET_DATA_TIMEOUT_SEC),
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    items = raw.get("data") if isinstance(raw, dict) else []
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        full_symbol = str(item.get("s") or "").strip()
        if not full_symbol or full_symbol.startswith("OTC:"):
            continue
        symbol = full_symbol.split(":")[-1].upper().strip()
        fields = item.get("d") if isinstance(item.get("d"), list) else []
        if len(fields) < 10:
            continue
        asset_type = str(fields[2] or "").strip().lower()
        if asset_type not in {"stock", "fund", "dr", "etf"}:
            continue
        session_pct = to_float(fields[8])
        if session_pct is None:
            continue
        market_cap = to_float(fields[6])
        out.append(
            {
                "symbol": symbol,
                "name": str(fields[1] or symbol),
                "asset_group": infer_after_hours_asset_group(asset_type),
                "price": finite_or(fields[7], 0),
                "percent_change": session_pct,
                "volume": finite_or(fields[5], 0),
                "market_cap": market_cap,
                "market_cap_bucket": market_cap_bucket(market_cap),
                "session_type": "post" if session == "postmarket" else "pre",
                "source_provider": "research_tradingview",
            }
        )
    return out


def persist_after_hours_snapshot(conn: sqlite3.Connection, snapshot_at: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    conn.execute("DELETE FROM after_hours_snapshot WHERE snapshot_at = ?", (snapshot_at,))
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO after_hours_snapshot
            (snapshot_at, symbol, name, asset_group, price, percent_change, volume, market_cap, market_cap_bucket, session_type, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_at,
                str(row.get("symbol") or ""),
                str(row.get("name") or ""),
                str(row.get("asset_group") or "stocks"),
                finite_or(row.get("price"), 0),
                to_float(row.get("percent_change")),
                finite_or(row.get("volume"), 0),
                to_float(row.get("market_cap")),
                str(row.get("market_cap_bucket") or ""),
                str(row.get("session_type") or ""),
                str(row.get("source_provider") or "research_tradingview"),
                now_iso(),
            ),
        )
    cutoff = (datetime.now(UTC) - timedelta(days=14)).isoformat()
    conn.execute("DELETE FROM after_hours_snapshot WHERE snapshot_at < ?", (cutoff,))


def load_latest_after_hours_snapshot(conn: sqlite3.Connection, limit: int = 20) -> tuple[str, list[dict[str, Any]]]:
    snap = conn.execute(
        "SELECT snapshot_at FROM after_hours_snapshot ORDER BY snapshot_at DESC LIMIT 1"
    ).fetchone()
    snapshot_at = str(snap[0] or "") if snap and snap[0] else ""
    if not snapshot_at:
        return "", []
    rows = conn.execute(
        """
        SELECT symbol, name, asset_group, price, percent_change, volume, market_cap, market_cap_bucket, session_type, source, updated_at
        FROM after_hours_snapshot
        WHERE snapshot_at = ?
        ORDER BY ABS(COALESCE(percent_change, 0)) DESC
        LIMIT ?
        """,
        (snapshot_at, int(limit)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "symbol": str(r[0] or ""),
                "name": str(r[1] or ""),
                "asset_group": str(r[2] or "stocks"),
                "price": finite_or(r[3], 0),
                "percent_change": to_float(r[4]),
                "volume": finite_or(r[5], 0),
                "market_cap": to_float(r[6]),
                "market_cap_bucket": str(r[7] or "") or None,
                "session_type": str(r[8] or ""),
                "source_provider": str(r[9] or "research_tradingview"),
                "updated_at": str(r[10] or ""),
            }
        )
    return snapshot_at, out


def build_after_hours_snapshot_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [
            ex.submit(fetch_tradingview_after_hours, "post", 300),
            ex.submit(fetch_tradingview_after_hours, "pre", 300),
        ]
        for fut in as_completed(futures):
            try:
                rows.extend(fut.result())
            except Exception:
                continue

    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        current = dedup.get(sym)
        if current is None:
            dedup[sym] = row
            continue
        if abs(float(row.get("percent_change") or 0)) > abs(float(current.get("percent_change") or 0)):
            dedup[sym] = row
    return list(dedup.values())


def _build_market_after_hours() -> dict[str, Any]:
    errors: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    snapshot_at = ""

    try:
        rows = build_after_hours_snapshot_rows()
    except Exception as exc:
        errors["research_fetch"] = str(exc)
        rows = []

    if rows:
        rows = [r for r in rows if r.get("percent_change") is not None]
        rows.sort(key=lambda x: abs(float(x.get("percent_change") or 0)), reverse=True)
        snapshot_at = datetime.now(UTC).replace(second=0, microsecond=0).isoformat()
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                persist_after_hours_snapshot(conn, snapshot_at, rows)
                conn.commit()
            finally:
                conn.close()
    else:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                snapshot_at, rows = load_latest_after_hours_snapshot(conn, limit=40)
            finally:
                conn.close()
        if not rows:
            return {
                "updated_at": now_iso(),
                "provider": "research_tradingview",
                "rows": [],
                "count": 0,
                "errors": errors or {"message": "No live or cached after-hours data available"},
            }
        # Keep stale fallback bounded.
        snap_dt = parse_iso_dt(snapshot_at)
        if snap_dt is not None:
            age_h = (datetime.now(UTC) - snap_dt).total_seconds() / 3600.0
            if age_h > max(1, AFTER_HOURS_SNAPSHOT_MAX_AGE_HOURS):
                errors["snapshot_age"] = f"latest_snapshot_older_than_{AFTER_HOURS_SNAPSHOT_MAX_AGE_HOURS}h"

    top_rows = rows[:20]
    payload = {
        "updated_at": now_iso(),
        "provider": "research_tradingview",
        "snapshot_at": snapshot_at,
        "rows": top_rows,
        "count": len(top_rows),
    }
    if errors:
        payload["errors"] = errors
    return payload
