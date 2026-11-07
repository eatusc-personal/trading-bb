"""Technical alert endpoint and its builder."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from ..config import SCREENER_HISTORY_WORKERS, SYMBOL_PATTERN
from ..indicators import compute_indicators
from ..market_data import fetch_history, validate_symbol
from ..swr import swr_serve
from ..utils import now_iso
from .market import market_overview

router = APIRouter()


def _build_alerts(symbols: str | None) -> dict[str, Any]:
    if symbols:
        universe = [validate_symbol(s) for s in symbols.split(",") if s.strip()]
    else:
        overview = market_overview()
        universe = []
        for key in ["active", "gainers"]:
            for row in overview.get(key, [])[:15]:
                sym = str(row.get("symbol") or "").upper()
                if sym and SYMBOL_PATTERN.match(sym):
                    universe.append(sym)
        universe = sorted(set(universe))

    # Fetch all histories in parallel — sequential fetching made this the slowest
    # endpoint on the Market tab (each cold history call is a provider round-trip).
    capped = universe[:40]
    histories: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=max(2, SCREENER_HISTORY_WORKERS)) as pool:
        futures = {pool.submit(fetch_history, sym, "1Y"): sym for sym in capped}
        for future in as_completed(futures):
            try:
                histories[futures[future]] = future.result()
            except HTTPException:
                continue
            except Exception:
                continue

    flags: list[dict[str, Any]] = []
    for sym in capped:
        hist = histories.get(sym)
        if hist is None:
            continue
        ind = compute_indicators(hist)
        latest = ind.iloc[-1]
        prev = ind.iloc[-2] if len(ind) > 1 else latest

        checks = []
        if pd.notna(latest["rsi14"]) and latest["rsi14"] < 30:
            checks.append("RSI < 30")
        if pd.notna(latest["rsi14"]) and latest["rsi14"] > 70:
            checks.append("RSI > 70")
        if (
            pd.notna(prev["sma50"])
            and prev["close"] <= prev["sma50"]
            and pd.notna(latest["sma50"])
            and latest["close"] > latest["sma50"]
        ):
            checks.append("Crossed Above 50 SMA")
        if (
            pd.notna(prev["sma50"])
            and prev["close"] >= prev["sma50"]
            and pd.notna(latest["sma50"])
            and latest["close"] < latest["sma50"]
        ):
            checks.append("Crossed Below 50 SMA")
        if pd.notna(latest["rvol"]) and latest["rvol"] >= 2:
            checks.append("Unusual Volume (RVOL >= 2)")
        if pd.notna(latest["daily_change_pct"]) and abs(latest["daily_change_pct"]) >= 5:
            checks.append("Large Daily Move (>= 5%)")

        if checks:
            flags.append(
                {
                    "symbol": sym,
                    "price": float(latest["close"]),
                    "change_pct": float(latest["daily_change_pct"]) if pd.notna(latest["daily_change_pct"]) else None,
                    "rsi14": float(latest["rsi14"]) if pd.notna(latest["rsi14"]) else None,
                    "alerts": checks,
                }
            )

    return {"rows": flags, "count": len(flags), "updated_at": now_iso()}


@router.get("/api/alerts")
def alerts(symbols: str | None = Query(default=None)) -> dict[str, Any]:
    if symbols:
        # Explicit symbol lists compute live — histories are cached, so this is fast.
        return _build_alerts(symbols)
    payload, meta = swr_serve("alerts:default", lambda: _build_alerts(None), refresh_after_sec=120)
    return {**payload, "cache": meta}
