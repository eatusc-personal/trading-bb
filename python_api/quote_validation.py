"""Quote-based ticker liveness checks used to validate social candidates,
backed by the in-memory cache and the quote_liveness_cache table."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from .cache import get_cached, set_cached
from .config import MARKET_DATA_PROVIDER
from .db import _social_db_conn, _social_db_lock
from .market_data import fetch_quote
from .providers import (
    is_quote_provider_auth_error,
    quote_provider_cooldown_remaining_sec,
    quote_provider_record_auth_failure,
    quote_provider_record_success,
)
from .symbol_master import resolve_symbol_validity
from .utils import now_iso, to_float


def is_symbol_quote_valid(symbol: str) -> bool:
    cache_key = f"social:quote_live:{symbol}:{MARKET_DATA_PROVIDER}"
    cached = get_cached(cache_key, 60)
    if cached is not None:
        return bool(cached)

    bucket_minute = int(time.time() // 60)
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            row = conn.execute(
                """
                SELECT is_live
                FROM quote_liveness_cache
                WHERE symbol = ? AND bucket_minute = ?
                """,
                (symbol, bucket_minute),
            ).fetchone()
            if row is not None:
                live = bool(int(row[0] or 0))
                set_cached(cache_key, live)
                return live
        finally:
            conn.close()

    valid = False
    last_price: float | None = None
    err: str | None = None
    provider = MARKET_DATA_PROVIDER
    try:
        df = fetch_quote([symbol], enrich=False)
        if not df.empty and "symbol" in df.columns:
            row = df[df["symbol"].astype(str).str.upper() == symbol]
            if not row.empty:
                r = row.iloc[0]
                last_price = to_float(r.get("last_price"))
                price = to_float(r.get("price"))
                provider = str(r.get("source_provider") or MARKET_DATA_PROVIDER)
                valid = bool((last_price is not None and last_price > 0) or (price is not None and price > 0))
    except Exception as exc:
        err = str(exc)

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO quote_liveness_cache
                (symbol, provider, bucket_minute, is_live, last_price, checked_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    provider,
                    bucket_minute,
                    1 if valid else 0,
                    last_price,
                    now_iso(),
                    err,
                ),
            )
            # Keep about 7 days of minute buckets.
            cutoff = bucket_minute - (7 * 24 * 60)
            conn.execute("DELETE FROM quote_liveness_cache WHERE bucket_minute < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()

    set_cached(cache_key, valid)
    return valid


def filter_valid_symbols(symbols: list[str]) -> tuple[set[str], dict[str, Any]]:
    unique = sorted(set(symbols))
    out: set[str] = set()
    provider_unavailable = False
    auth_error_count = 0
    errors: list[str] = []
    quote_provider_cooldown_sec = quote_provider_cooldown_remaining_sec()

    today = datetime.now(UTC).date().isoformat()
    unresolved: list[str] = []
    for sym in unique:
        validity = resolve_symbol_validity(sym, today)
        if not bool(validity.get("is_valid")):
            continue
        resolved = str(validity.get("resolved_symbol") or sym).upper()
        cache_key = f"social:quote_live:{resolved}:{MARKET_DATA_PROVIDER}"
        cached = get_cached(cache_key, 60)
        if cached is None:
            unresolved.append(resolved)
        elif bool(cached):
            out.add(resolved)

    if quote_provider_cooldown_sec > 0:
        diagnostics = {
            "provider_unavailable": True,
            "auth_error_count": 1,
            "errors": [f"quote_provider_auth_cooldown_active:{quote_provider_cooldown_sec}s"],
            "quote_provider_cooldown_sec": quote_provider_cooldown_sec,
        }
        return out, diagnostics

    unresolved = sorted(set(unresolved))
    chunk_size = 40
    for i in range(0, len(unresolved), chunk_size):
        chunk = unresolved[i : i + chunk_size]
        try:
            df = fetch_quote(chunk, enrich=False)
            quote_provider_record_success()
            seen: set[str] = set()
            bucket_minute = int(time.time() // 60)
            updates: list[tuple[str, str, bool, float | None, str | None]] = []
            if not df.empty and "symbol" in df.columns:
                for _, r in df.iterrows():
                    sym = str(r.get("symbol") or "").upper()
                    if not sym:
                        continue
                    seen.add(sym)
                    provider = str(r.get("source_provider") or MARKET_DATA_PROVIDER)
                    last_price = to_float(r.get("last_price"))
                    price = to_float(r.get("price"))
                    valid = bool((last_price is not None and last_price > 0) or (price is not None and price > 0))
                    resolved_last = last_price if last_price is not None else price
                    updates.append((sym, provider, valid, resolved_last, None))
                    set_cached(f"social:quote_live:{sym}:{MARKET_DATA_PROVIDER}", valid)
                    if valid:
                        out.add(sym)
            for sym in chunk:
                if sym not in seen:
                    updates.append((sym, MARKET_DATA_PROVIDER, False, None, "not_returned"))
                    set_cached(f"social:quote_live:{sym}:{MARKET_DATA_PROVIDER}", False)
            with _social_db_lock:
                conn = _social_db_conn()
                try:
                    for sym, provider, valid, live_price, err in updates:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO quote_liveness_cache
                            (symbol, provider, bucket_minute, is_live, last_price, checked_at, error)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                sym,
                                provider,
                                bucket_minute,
                                1 if valid else 0,
                                live_price,
                                now_iso(),
                                err,
                            ),
                        )
                    cutoff = bucket_minute - (7 * 24 * 60)
                    conn.execute("DELETE FROM quote_liveness_cache WHERE bucket_minute < ?", (cutoff,))
                    conn.commit()
                finally:
                    conn.close()
            time.sleep(0.15)
        except Exception as exc:
            if is_quote_provider_auth_error(exc):
                provider_unavailable = True
                auth_error_count += 1
                errors.append(str(exc))
                quote_provider_record_auth_failure()
                break
            for sym in chunk:
                if is_symbol_quote_valid(sym):
                    out.add(sym)
            time.sleep(0.15)

    diagnostics = {
        "provider_unavailable": provider_unavailable,
        "auth_error_count": auth_error_count,
        "errors": errors[:3],
        "quote_provider_cooldown_sec": quote_provider_cooldown_remaining_sec(),
    }
    return out, diagnostics
