"""Symbol master + alias resolution: loads the Nasdaq/other-listed symbol
universe, tracks point-in-time snapshots and aliases in SQLite, and resolves
symbol validity as-of a date. Moved verbatim from main.py."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .cache import get_cached, set_cached
from .config import *  # noqa: F401,F403 -- symbol master urls, alias seed path
from .db import _social_db_conn, _social_db_lock
from .net import fetch_text_url
from .utils import now_iso

logger = logging.getLogger("openbb_api.symbol_master")


def refresh_symbol_alias_seed(force: bool = False) -> dict[str, Any]:
    today = datetime.now(UTC).date().isoformat()
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            if not force:
                refreshed_row = conn.execute(
                    "SELECT value FROM symbol_master_meta WHERE key = ?",
                    ("alias_seed_refreshed_on",),
                ).fetchone()
                if refreshed_row and str(refreshed_row[0] or "") == today:
                    count_row = conn.execute("SELECT COUNT(*) FROM symbol_alias").fetchone()
                    return {
                        "refreshed": False,
                        "reason": "already_refreshed_today",
                        "seed_path": SYMBOL_ALIAS_SEED_PATH,
                        "alias_count": int(count_row[0] or 0) if count_row else 0,
                        "refreshed_on": today,
                    }

            try:
                with open(SYMBOL_ALIAS_SEED_PATH, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except FileNotFoundError:
                raw = []
            except Exception as exc:
                logger.warning("Symbol alias seed unreadable (%s): %s", SYMBOL_ALIAS_SEED_PATH, exc)
                raw = []

            entries = raw if isinstance(raw, list) else []
            normalized: list[tuple[str, str, str, str | None, str]] = []
            for row in entries:
                if not isinstance(row, dict):
                    continue
                alias_symbol = str(row.get("alias_symbol") or "").upper().strip()
                canonical_symbol = str(row.get("canonical_symbol") or "").upper().strip()
                effective_from = normalize_as_of_date(str(row.get("effective_from") or "2000-01-01"))
                effective_to_raw = str(row.get("effective_to") or "").strip()
                effective_to = normalize_as_of_date(effective_to_raw) if effective_to_raw else None
                source = str(row.get("source") or "seed").strip() or "seed"
                if not alias_symbol or not canonical_symbol:
                    continue
                if alias_symbol == canonical_symbol:
                    continue
                normalized.append((alias_symbol, canonical_symbol, effective_from, effective_to, source))

            conn.execute("DELETE FROM symbol_alias WHERE source IN ('seed', 'seed_manual')")
            for alias_symbol, canonical_symbol, effective_from, effective_to, source in normalized:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO symbol_alias
                    (alias_symbol, canonical_symbol, effective_from, effective_to, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alias_symbol,
                        canonical_symbol,
                        effective_from,
                        effective_to,
                        source,
                        now_iso(),
                    ),
                )

            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_master_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                ("alias_seed_refreshed_on", today, now_iso()),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_master_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                ("alias_seed_refreshed_count", str(len(normalized)), now_iso()),
            )
            conn.commit()
            return {
                "refreshed": True,
                "seed_path": SYMBOL_ALIAS_SEED_PATH,
                "alias_count": len(normalized),
                "refreshed_on": today,
            }
        finally:
            conn.close()

def symbol_alias_health() -> dict[str, Any]:
    refresh_symbol_alias_seed(force=False)
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            total_row = conn.execute("SELECT COUNT(*) FROM symbol_alias").fetchone()
            sample_rows = conn.execute(
                """
                SELECT alias_symbol, canonical_symbol, effective_from, effective_to, source
                FROM symbol_alias
                ORDER BY alias_symbol ASC
                LIMIT 30
                """
            ).fetchall()
            refreshed_on = conn.execute(
                "SELECT value FROM symbol_master_meta WHERE key = ?",
                ("alias_seed_refreshed_on",),
            ).fetchone()
            refreshed_count = conn.execute(
                "SELECT value FROM symbol_master_meta WHERE key = ?",
                ("alias_seed_refreshed_count",),
            ).fetchone()
        finally:
            conn.close()

    return {
        "updated_at": now_iso(),
        "seed_path": SYMBOL_ALIAS_SEED_PATH,
        "alias_count": int(total_row[0] or 0) if total_row else 0,
        "last_refreshed_on": str(refreshed_on[0] or "") if refreshed_on else "",
        "last_refreshed_count": int(refreshed_count[0] or 0) if refreshed_count else 0,
        "sample": [
            {
                "alias_symbol": str(r[0] or ""),
                "canonical_symbol": str(r[1] or ""),
                "effective_from": str(r[2] or ""),
                "effective_to": str(r[3] or "") if r[3] is not None else None,
                "source": str(r[4] or ""),
            }
            for r in sample_rows
        ],
    }

def load_symbol_master() -> tuple[dict[str, dict[str, Any]], str, str]:
    refresh_symbol_alias_seed(force=False)
    cache_key = "social:symbol_master:v2"
    cached = get_cached(cache_key, 24 * 3600)
    if cached is not None:
        return cached

    def fetch_remote_symbol_master() -> dict[str, dict[str, Any]]:
        symbols: dict[str, dict[str, Any]] = {}

        # NASDAQ listed
        try:
            txt = fetch_text_url(SYMBOL_MASTER_URLS[0], timeout=10)
            lines = [ln for ln in txt.splitlines() if ln and not ln.startswith("File Creation Time")]
            if lines:
                headers = lines[0].split("|")
                for ln in lines[1:]:
                    parts = ln.split("|")
                    if len(parts) != len(headers):
                        continue
                    row = dict(zip(headers, parts))
                    sym = str(row.get("Symbol") or "").strip().upper()
                    if not sym:
                        continue
                    name = str(row.get("Security Name") or "")
                    if row.get("Test Issue") == "Y":
                        continue
                    if any(x in name.lower() for x in ["warrant", "rights", "unit", "preferred"]):
                        continue
                    symbols[sym] = {
                        "symbol": sym,
                        "name": name,
                        "exchange": "NASDAQ",
                        "is_etf": row.get("ETF") == "Y",
                        "asset_type": "etf" if row.get("ETF") == "Y" else "equity",
                    }
        except Exception as exc:
            logger.warning("Symbol master fetch failed (%s): %s", SYMBOL_MASTER_URLS[0], exc)

        # NYSE/AMEX/other listed
        try:
            txt = fetch_text_url(SYMBOL_MASTER_URLS[1], timeout=10)
            lines = [ln for ln in txt.splitlines() if ln and not ln.startswith("File Creation Time")]
            if lines:
                headers = lines[0].split("|")
                for ln in lines[1:]:
                    parts = ln.split("|")
                    if len(parts) != len(headers):
                        continue
                    row = dict(zip(headers, parts))
                    sym = str(row.get("ACT Symbol") or row.get("NASDAQ Symbol") or "").strip().upper()
                    if not sym:
                        continue
                    name = str(row.get("Security Name") or "")
                    if row.get("Test Issue") == "Y":
                        continue
                    if any(x in name.lower() for x in ["warrant", "rights", "unit", "preferred"]):
                        continue
                    ex = str(row.get("Exchange") or "")
                    symbols[sym] = {
                        "symbol": sym,
                        "name": name,
                        "exchange": ex or "OTHER",
                        "is_etf": row.get("ETF") == "Y",
                        "asset_type": "etf" if row.get("ETF") == "Y" else "equity",
                    }
        except Exception as exc:
            logger.warning("Symbol master fetch failed (%s): %s", SYMBOL_MASTER_URLS[1], exc)
        return symbols

    def load_snapshot(conn: sqlite3.Connection, snapshot_date: str) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT symbol, name, exchange, is_etf, asset_type
            FROM symbol_master_snapshot
            WHERE snapshot_date = ?
            """,
            (snapshot_date,),
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for sym, name, ex, is_etf, asset_type in rows:
            symbol = str(sym or "").upper()
            if not symbol:
                continue
            out[symbol] = {
                "symbol": symbol,
                "name": str(name or ""),
                "exchange": str(ex or ""),
                "is_etf": bool(int(is_etf or 0)),
                "asset_type": str(asset_type or "equity"),
            }
        return out

    def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO symbol_master_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, now_iso()),
        )

    def persist_snapshot(conn: sqlite3.Connection, snapshot_date: str, symbols: dict[str, dict[str, Any]]) -> None:
        if not symbols:
            return
        for sym, item in symbols.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_master_snapshot
                (snapshot_date, symbol, name, exchange, is_etf, asset_type, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_date,
                    sym,
                    str(item.get("name") or ""),
                    str(item.get("exchange") or ""),
                    1 if bool(item.get("is_etf")) else 0,
                    str(item.get("asset_type") or "equity"),
                    "nasdaq_trader",
                ),
            )
        set_meta(conn, "latest_snapshot_date", snapshot_date)
        set_meta(conn, "latest_snapshot_source", "remote_refresh")
        set_meta(conn, "latest_snapshot_rows", str(len(symbols)))
        # Keep roughly 18 months of daily snapshots.
        cutoff = (datetime.now(UTC) - timedelta(days=540)).date().isoformat()
        conn.execute("DELETE FROM symbol_master_snapshot WHERE snapshot_date < ?", (cutoff,))

    snapshot_date = datetime.now(UTC).date().isoformat()
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            today_symbols = load_snapshot(conn, snapshot_date)
            if len(today_symbols) >= 2000:
                result = (today_symbols, snapshot_date, "sqlite_snapshot")
                set_cached(cache_key, result)
                return result

            remote_symbols = fetch_remote_symbol_master()
            if remote_symbols:
                persist_snapshot(conn, snapshot_date, remote_symbols)
                conn.commit()
                result = (remote_symbols, snapshot_date, "remote_refresh")
                set_cached(cache_key, result)
                return result

            latest = conn.execute(
                "SELECT value FROM symbol_master_meta WHERE key = ?",
                ("latest_snapshot_date",),
            ).fetchone()
            latest_date = str(latest[0]) if latest and latest[0] else ""
            if latest_date:
                latest_symbols = load_snapshot(conn, latest_date)
                if latest_symbols:
                    try:
                        age_days = (datetime.now(UTC).date() - datetime.fromisoformat(latest_date).date()).days
                    except Exception:
                        age_days = -1
                    if age_days > 7:
                        logger.warning(
                            "Symbol master remote refresh failed; serving stale snapshot %s (%d days old)",
                            latest_date,
                            age_days,
                        )
                    set_meta(conn, "latest_snapshot_source", "sqlite_fallback")
                    set_meta(conn, "latest_snapshot_rows", str(len(latest_symbols)))
                    conn.commit()
                    result = (latest_symbols, latest_date, "sqlite_fallback")
                    set_cached(cache_key, result)
                    return result

            # Last-resort fallback to today rows (possibly partial)
            logger.warning("Symbol master remote refresh failed and no prior snapshot; serving partial rows (%d)", len(today_symbols))
            set_meta(conn, "latest_snapshot_source", "sqlite_partial")
            set_meta(conn, "latest_snapshot_rows", str(len(today_symbols)))
            conn.commit()
            result = (today_symbols, snapshot_date, "sqlite_partial")
            set_cached(cache_key, result)
            return result
        finally:
            conn.close()

def symbol_master_health() -> dict[str, Any]:
    # Ensure cache/snapshot path is exercised before reading health.
    symbols, snapshot_date, source = load_symbol_master()
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            latest_meta_rows = conn.execute(
                "SELECT key, value, updated_at FROM symbol_master_meta WHERE key IN ('latest_snapshot_date', 'latest_snapshot_source', 'latest_snapshot_rows')"
            ).fetchall()
            latest_meta: dict[str, dict[str, str]] = {}
            for key, value, updated_at in latest_meta_rows:
                latest_meta[str(key)] = {"value": str(value or ""), "updated_at": str(updated_at or "")}

            snapshot_count_row = conn.execute("SELECT COUNT(DISTINCT snapshot_date) FROM symbol_master_snapshot").fetchone()
            total_rows_row = conn.execute("SELECT COUNT(*) FROM symbol_master_snapshot").fetchone()
            latest_rows_row = conn.execute(
                "SELECT COUNT(*) FROM symbol_master_snapshot WHERE snapshot_date = ?",
                (snapshot_date,),
            ).fetchone()
            oldest_snapshot_row = conn.execute("SELECT MIN(snapshot_date) FROM symbol_master_snapshot").fetchone()
        finally:
            conn.close()

    days_since_snapshot: int | None = None
    try:
        days_since_snapshot = (datetime.now(UTC).date() - datetime.fromisoformat(snapshot_date).date()).days
    except Exception:
        days_since_snapshot = None

    return {
        "updated_at": now_iso(),
        "current_snapshot_date": snapshot_date,
        "current_snapshot_source": source,
        "current_snapshot_symbol_count": len(symbols),
        "days_since_snapshot": days_since_snapshot,
        "snapshot_stale": bool(days_since_snapshot is not None and days_since_snapshot > 7),
        "snapshot_count": int(snapshot_count_row[0] or 0) if snapshot_count_row else 0,
        "snapshot_total_rows": int(total_rows_row[0] or 0) if total_rows_row else 0,
        "latest_snapshot_rows": int(latest_rows_row[0] or 0) if latest_rows_row else 0,
        "oldest_snapshot_date": str(oldest_snapshot_row[0] or "") if oldest_snapshot_row else "",
        "meta": latest_meta,
    }

def symbol_master_snapshots(limit: int = 60) -> dict[str, Any]:
    cap = max(1, min(365, int(limit)))
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            rows = conn.execute(
                """
                SELECT snapshot_date, COUNT(*) AS rows_count
                FROM symbol_master_snapshot
                GROUP BY snapshot_date
                ORDER BY snapshot_date DESC
                LIMIT ?
                """,
                (cap,),
            ).fetchall()
        finally:
            conn.close()
    items = [{"snapshot_date": str(r[0] or ""), "rows": int(r[1] or 0)} for r in rows]
    return {"updated_at": now_iso(), "count": len(items), "rows": items}

def normalize_as_of_date(as_of: str | None = None) -> str:
    if not as_of:
        return datetime.now(UTC).date().isoformat()
    try:
        return datetime.fromisoformat(as_of).date().isoformat()
    except Exception:
        return datetime.now(UTC).date().isoformat()

def load_symbol_master_at_date(as_of_date: str) -> tuple[dict[str, dict[str, Any]], str, str]:
    target_date = normalize_as_of_date(as_of_date)
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            row = conn.execute(
                """
                SELECT snapshot_date
                FROM symbol_master_snapshot
                WHERE snapshot_date <= ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (target_date,),
            ).fetchone()
            snapshot_date = str(row[0]) if row and row[0] else ""
            if snapshot_date:
                rows = conn.execute(
                    """
                    SELECT symbol, name, exchange, is_etf, asset_type
                    FROM symbol_master_snapshot
                    WHERE snapshot_date = ?
                    """,
                    (snapshot_date,),
                ).fetchall()
                out: dict[str, dict[str, Any]] = {}
                for sym, name, ex, is_etf, asset_type in rows:
                    symbol = str(sym or "").upper()
                    if not symbol:
                        continue
                    out[symbol] = {
                        "symbol": symbol,
                        "name": str(name or ""),
                        "exchange": str(ex or ""),
                        "is_etf": bool(int(is_etf or 0)),
                        "asset_type": str(asset_type or "equity"),
                    }
                if out:
                    return out, snapshot_date, "historical_snapshot"
        finally:
            conn.close()
    return load_symbol_master()

def resolve_symbol_alias(symbol: str, as_of_date: str) -> tuple[str, str | None]:
    candidate = str(symbol or "").upper().strip()
    if not candidate:
        return "", None
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            row = conn.execute(
                """
                SELECT canonical_symbol
                FROM symbol_alias
                WHERE alias_symbol = ?
                  AND effective_from <= ?
                  AND (effective_to IS NULL OR effective_to >= ?)
                ORDER BY effective_from DESC
                LIMIT 1
                """,
                (candidate, as_of_date, as_of_date),
            ).fetchone()
            if row and row[0]:
                canonical = str(row[0]).upper().strip()
                if canonical:
                    return canonical, candidate
        finally:
            conn.close()
    return candidate, None

def resolve_symbol_validity(symbol: str, as_of_date: str) -> dict[str, Any]:
    refresh_symbol_alias_seed(force=False)
    as_of = normalize_as_of_date(as_of_date)
    input_symbol = str(symbol or "").upper().strip()
    if not input_symbol:
        return {
            "input_symbol": "",
            "resolved_symbol": "",
            "is_valid": False,
            "as_of_date": as_of,
            "snapshot_date": "",
            "snapshot_source": "none",
            "alias_applied": False,
            "alias_from": None,
            "cache": "none",
            "checked_at": now_iso(),
        }

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            cached = conn.execute(
                """
                SELECT is_valid, resolved_symbol, snapshot_date, snapshot_source, checked_at
                FROM symbol_validity_cache
                WHERE symbol = ? AND as_of_date = ?
                """,
                (input_symbol, as_of),
            ).fetchone()
            if cached:
                return {
                    "input_symbol": input_symbol,
                    "resolved_symbol": str(cached[1] or input_symbol),
                    "is_valid": bool(int(cached[0] or 0)),
                    "as_of_date": as_of,
                    "snapshot_date": str(cached[2] or ""),
                    "snapshot_source": str(cached[3] or ""),
                    "alias_applied": str(cached[1] or input_symbol) != input_symbol,
                    "alias_from": input_symbol if str(cached[1] or input_symbol) != input_symbol else None,
                    "cache": "sqlite_validity",
                    "checked_at": str(cached[4] or ""),
                }
        finally:
            conn.close()

    symbol_master, snapshot_date, snapshot_source = load_symbol_master_at_date(as_of)
    resolved_symbol, alias_from = resolve_symbol_alias(input_symbol, as_of)
    is_valid = resolved_symbol in symbol_master
    checked_at = now_iso()

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_validity_cache
                (symbol, as_of_date, is_valid, resolved_symbol, snapshot_date, snapshot_source, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    input_symbol,
                    as_of,
                    1 if is_valid else 0,
                    resolved_symbol,
                    snapshot_date,
                    snapshot_source,
                    checked_at,
                ),
            )
            # Keep about 6 months of validity lookups.
            cutoff = (datetime.now(UTC) - timedelta(days=180)).date().isoformat()
            conn.execute("DELETE FROM symbol_validity_cache WHERE as_of_date < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()

    return {
        "input_symbol": input_symbol,
        "resolved_symbol": resolved_symbol,
        "is_valid": is_valid,
        "as_of_date": as_of,
        "snapshot_date": snapshot_date,
        "snapshot_source": snapshot_source,
        "alias_applied": alias_from is not None,
        "alias_from": alias_from,
        "cache": "computed",
        "checked_at": checked_at,
    }
