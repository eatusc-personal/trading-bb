"""Screener endpoints: the filtered screener and top-volume trigger scan."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..cache import get_cached, set_cached
from ..config import MARKET_DATA_PROVIDER, SCREENER_HISTORY_WORKERS, SYMBOL_PATTERN
from ..indicators import compute_indicators, compute_technical_trigger_flags, trigger_labels
from ..market_data import build_universe, fetch_history, fetch_quote
from ..utils import finite_or, normalize_pct, now_iso

router = APIRouter()


class ScreenerRequest(BaseModel):
    # Optional explicit universe (e.g. watchlist or an industry peer list).
    # When provided it replaces the discovery-built universe.
    symbols: list[str] | None = None
    ticker_contains: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    min_volume: float | None = None
    min_percent_change: float | None = None
    max_percent_change: float | None = None
    rsi_min: float | None = None
    rsi_max: float | None = None
    above_sma20: bool | None = None
    above_sma50: bool | None = None
    above_sma200: bool | None = None
    limit: int = Field(default=100, ge=1, le=300)


@router.post("/api/screener")
def screener(payload: ScreenerRequest) -> dict[str, Any]:
    if payload.symbols:
        explicit: list[str] = []
        for raw in payload.symbols[:100]:
            sym = str(raw or "").upper().strip()
            if sym and SYMBOL_PATTERN.match(sym) and sym not in explicit:
                explicit.append(sym)
        universe = explicit
    else:
        universe = build_universe()
    contains = payload.ticker_contains.strip().upper() if payload.ticker_contains else None

    if contains:
        universe = [s for s in universe if contains in s]

    if not universe:
        return {"rows": [], "count": 0, "updated_at": now_iso(), "universe_count": 0}

    quotes = fetch_quote(universe, enrich=False)
    quote_map = {str(r["symbol"]).upper(): r for _, r in quotes.iterrows()}

    quote_candidates: list[dict[str, Any]] = []
    for sym in universe:
        q = quote_map.get(sym)
        if q is None:
            continue
        last_price = finite_or(q.get("last_price"), 0)
        volume = finite_or(q.get("volume"), 0)
        prev_close = finite_or(q.get("prev_close"), 0)
        raw_pct = normalize_pct(q.get("percent_change"))
        pct_change = (
            float(raw_pct)
            if raw_pct is not None
            else ((last_price - prev_close) / prev_close * 100) if prev_close else 0.0
        )

        if payload.min_price is not None and last_price < payload.min_price:
            continue
        if payload.max_price is not None and last_price > payload.max_price:
            continue
        if payload.min_volume is not None and volume < payload.min_volume:
            continue
        if payload.min_percent_change is not None and pct_change < payload.min_percent_change:
            continue
        if payload.max_percent_change is not None and pct_change > payload.max_percent_change:
            continue

        quote_candidates.append(
            {
                "symbol": sym,
                "name": str(q.get("name") or sym),
                "price": finite_or(last_price, 0),
                "volume": finite_or(volume, 0),
                "percent_change": finite_or(pct_change, 0),
                "quote_provider": str(q.get("source_provider") or ""),
            }
        )

    if not quote_candidates:
        return {"rows": [], "count": 0, "updated_at": now_iso(), "universe_count": len(universe)}

    def enrich_with_history(quote_row: dict[str, Any]) -> dict[str, Any] | None:
        sym = str(quote_row["symbol"])
        try:
            hist = fetch_history(sym, "1Y")
            ind = compute_indicators(hist)
            latest = ind.iloc[-1]
            history_provider = str(hist.attrs.get("source_provider") or MARKET_DATA_PROVIDER)
        except HTTPException:
            return None

        last_price = finite_or(quote_row.get("price"), finite_or(latest.get("close"), 0))
        row = {
            "symbol": sym,
            "name": str(quote_row["name"]),
            "price": finite_or(last_price, 0),
            "volume": finite_or(quote_row.get("volume"), finite_or(latest.get("volume"), 0)),
            "percent_change": finite_or(quote_row.get("percent_change"), 0),
            "quote_provider": str(quote_row.get("quote_provider") or ""),
            "history_provider": history_provider,
            "rsi14": float(latest["rsi14"]) if pd.notna(latest["rsi14"]) else None,
            "sma20": float(latest["sma20"]) if pd.notna(latest["sma20"]) else None,
            "sma50": float(latest["sma50"]) if pd.notna(latest["sma50"]) else None,
            "sma200": float(latest["sma200"]) if pd.notna(latest["sma200"]) else None,
            "above_sma20": bool(pd.notna(latest["sma20"]) and last_price > latest["sma20"]),
            "above_sma50": bool(pd.notna(latest["sma50"]) and last_price > latest["sma50"]),
            "above_sma200": bool(pd.notna(latest["sma200"]) and last_price > latest["sma200"]),
            "rvol": float(latest["rvol"]) if pd.notna(latest["rvol"]) else None,
            "avg_volume20": float(latest["avg_volume20"]) if pd.notna(latest["avg_volume20"]) else None,
        }
        if payload.min_price is not None and row["price"] < payload.min_price:
            return None
        if payload.max_price is not None and row["price"] > payload.max_price:
            return None
        if payload.min_volume is not None and row["volume"] < payload.min_volume:
            return None
        if payload.min_percent_change is not None and row["percent_change"] < payload.min_percent_change:
            return None
        if payload.max_percent_change is not None and row["percent_change"] > payload.max_percent_change:
            return None
        if payload.rsi_min is not None and (row["rsi14"] is None or row["rsi14"] < payload.rsi_min):
            return None
        if payload.rsi_max is not None and (row["rsi14"] is None or row["rsi14"] > payload.rsi_max):
            return None
        if payload.above_sma20 is not None and row["above_sma20"] != payload.above_sma20:
            return None
        if payload.above_sma50 is not None and row["above_sma50"] != payload.above_sma50:
            return None
        if payload.above_sma200 is not None and row["above_sma200"] != payload.above_sma200:
            return None
        return row

    rows: list[dict[str, Any]] = []
    workers = max(1, min(SCREENER_HISTORY_WORKERS, len(quote_candidates)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(enrich_with_history, q) for q in quote_candidates]
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception:
                continue
            if row is not None:
                rows.append(row)

    rows.sort(key=lambda x: (x["percent_change"], x["volume"]), reverse=True)
    rows = rows[: payload.limit]

    return {
        "rows": rows,
        "count": len(rows),
        "updated_at": now_iso(),
        "provider": MARKET_DATA_PROVIDER,
        "source_providers": quotes.attrs.get("source_providers", []),
        "universe_count": len(universe),
    }


@router.get("/api/screener/top-volume-triggers")
def screener_top_volume_triggers(
    limit: int = Query(default=20, ge=5, le=80),
    universe_size: int = Query(default=120, ge=20, le=400),
    min_trigger_rows: int = Query(default=8, ge=0, le=40),
) -> dict[str, Any]:
    universe = build_universe()
    if not universe:
        return {
            "rows": [],
            "count": 0,
            "updated_at": now_iso(),
            "universe_count": 0,
            "checked_count": 0,
        }

    quotes = fetch_quote(universe, enrich=False)
    quote_rows: list[dict[str, Any]] = []
    for _, r in quotes.iterrows():
        sym = str(r.get("symbol") or "").upper().strip()
        if sym and sym in universe:
            quote_rows.append(
                {
                    "symbol": sym,
                    "name": str(r.get("name") or sym),
                    "last_price": finite_or(r.get("last_price"), 0),
                    "prev_close": finite_or(r.get("prev_close"), 0),
                    "volume": finite_or(r.get("volume"), 0),
                    "quote_provider": str(r.get("source_provider") or ""),
                }
            )

    quote_rows.sort(key=lambda x: x["volume"], reverse=True)
    selected = quote_rows[:universe_size]

    def load_row(q: dict[str, Any]) -> dict[str, Any]:
        sym = q["symbol"]
        prev_close = finite_or(q.get("prev_close"), 0)
        base_price = finite_or(q.get("last_price"), 0)
        base_pct_change = ((base_price - prev_close) / prev_close * 100) if prev_close else 0.0
        history_fail_key = f"screener:top_volume:history_fail:{sym}"

        def fallback_row(error_text: str) -> dict[str, Any]:
            return {
                "symbol": sym,
                "name": q["name"],
                "price": finite_or(base_price, 0),
                "volume": finite_or(q.get("volume"), 0),
                "percent_change": finite_or(base_pct_change, 0),
                "rsi14": None,
                "rvol": None,
                "has_triggers": False,
                "trigger_count": 0,
                "triggers": [],
                "quote_provider": str(q.get("quote_provider") or ""),
                "history_provider": "",
                "history_available": False,
                "analysis_error": error_text,
            }

        cooldown = get_cached(history_fail_key, 15 * 60)
        if isinstance(cooldown, dict) and str(cooldown.get("status") or "") == "fail":
            return fallback_row(str(cooldown.get("detail") or "recent_history_failure"))

        try:
            hist = fetch_history(sym, "1Y")
            ind = compute_indicators(hist)
            latest = ind.iloc[-1]
            flags = compute_technical_trigger_flags(ind)
            active_triggers = trigger_labels(flags)
            history_provider = str(hist.attrs.get("source_provider") or MARKET_DATA_PROVIDER)
            price = finite_or(q.get("last_price"), finite_or(latest.get("close"), 0))
            pct_change = ((price - prev_close) / prev_close * 100) if prev_close else float(latest["daily_change_pct"] or 0)
            set_cached(history_fail_key, {"status": "ok"})

            return {
                "symbol": sym,
                "name": q["name"],
                "price": finite_or(price, 0),
                "volume": finite_or(q.get("volume"), finite_or(latest.get("volume"), 0)),
                "percent_change": finite_or(pct_change, 0),
                "rsi14": float(latest["rsi14"]) if pd.notna(latest["rsi14"]) else None,
                "rvol": float(latest["rvol"]) if pd.notna(latest["rvol"]) else None,
                "has_triggers": bool(active_triggers),
                "trigger_count": len(active_triggers),
                "triggers": active_triggers,
                "quote_provider": str(q.get("quote_provider") or ""),
                "history_provider": history_provider,
                "history_available": True,
                "analysis_error": None,
            }
        except HTTPException as exc:
            detail = str(exc.detail)
            set_cached(history_fail_key, {"status": "fail", "detail": detail})
            return fallback_row(detail)
        except Exception as exc:
            detail = str(exc)
            set_cached(history_fail_key, {"status": "fail", "detail": detail})
            return fallback_row(detail)

    def process_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not candidates:
            return []
        out_rows: list[dict[str, Any]] = []
        workers = max(1, min(SCREENER_HISTORY_WORKERS, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(load_row, q) for q in candidates]
            for future in as_completed(futures):
                try:
                    row = future.result()
                except Exception:
                    continue
                out_rows.append(row)
        return out_rows

    rows: list[dict[str, Any]] = process_candidates(selected)
    checked_count = len(selected)

    # If the first volume slice has too few trigger hits, expand the scan window
    # so the visible table is more likely to contain actionable triggers.
    current_trigger_hits = sum(1 for r in rows if int(r.get("trigger_count") or 0) > 0)
    if current_trigger_hits < min_trigger_rows and len(quote_rows) > checked_count:
        extra_cap = min(len(quote_rows), max(universe_size, 220))
        extra_candidates = quote_rows[checked_count:extra_cap]
        rows.extend(process_candidates(extra_candidates))
        checked_count = extra_cap

    rows.sort(
        key=lambda x: (
            int(x.get("trigger_count") or 0),
            1 if bool(x.get("history_available")) else 0,
            float(x.get("volume") or 0),
        ),
        reverse=True,
    )
    rows = rows[:limit]
    analyzed_count = sum(1 for r in rows if bool(r.get("history_available")))
    history_failure_rows = [r for r in rows if not bool(r.get("history_available"))]
    triggered_count = sum(1 for r in rows if int(r.get("trigger_count") or 0) > 0)
    return {
        "rows": rows,
        "count": len(rows),
        "updated_at": now_iso(),
        "provider": MARKET_DATA_PROVIDER,
        "source_providers": quotes.attrs.get("source_providers", []),
        "universe_count": len(universe),
        "checked_count": checked_count,
        "analyzed_count": analyzed_count,
        "triggered_count": triggered_count,
        "history_failures": len(history_failure_rows),
        "history_failure_symbols": [str(r.get("symbol") or "") for r in history_failure_rows[:20]],
    }
