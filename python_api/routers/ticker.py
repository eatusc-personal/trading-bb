"""Per-ticker endpoints: historical bars, technicals and quote."""

from __future__ import annotations

from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from ..config import MARKET_DATA_PROVIDER
from ..indicators import compute_indicators
from ..market_data import fetch_history, fetch_quote, range_to_start, validate_symbol
from ..utils import finite_or, normalize_pct, now_iso

router = APIRouter()


@router.get("/api/ticker/{symbol}/historical")
def ticker_historical(symbol: str, range: str = Query(default="1Y")) -> dict[str, Any]:
    sym = validate_symbol(symbol)
    df = fetch_history(sym, range)
    provider = str(df.attrs.get("source_provider") or MARKET_DATA_PROVIDER)

    records = []
    for idx, row in df.iterrows():
        records.append(
            {
                "date": idx.date().isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )

    preview = records[-20:]
    return {
        "symbol": sym,
        "range": range.upper(),
        "updated_at": now_iso(),
        "provider": provider,
        "rows": len(records),
        "data": records,
        "preview": preview,
    }


@router.get("/api/ticker/{symbol}/technicals")
def ticker_technicals(symbol: str, range: str = Query(default="1Y")) -> dict[str, Any]:
    sym = validate_symbol(symbol)
    # 1Y can be too short for mature 200SMA on some assets; fetch 5Y source and cut later.
    source_df = fetch_history(sym, "5Y")
    provider = str(source_df.attrs.get("source_provider") or MARKET_DATA_PROVIDER)
    ind_df = compute_indicators(source_df)

    date_cutoff = pd.to_datetime(range_to_start(range))
    view_df = ind_df[ind_df.index >= date_cutoff]
    if view_df.empty:
        view_df = ind_df.tail(252)

    latest = ind_df.iloc[-1]
    prev = ind_df.iloc[-2] if len(ind_df) > 1 else latest

    flags = {
        "rsi_overbought": bool(pd.notna(latest["rsi14"]) and latest["rsi14"] > 70),
        "rsi_oversold": bool(pd.notna(latest["rsi14"]) and latest["rsi14"] < 30),
        "above_sma50": bool(pd.notna(latest["sma50"]) and latest["close"] > latest["sma50"]),
        "above_sma200": bool(pd.notna(latest["sma200"]) and latest["close"] > latest["sma200"]),
        "bullish_crossover_50": bool(
            pd.notna(prev["sma50"])
            and pd.notna(prev["close"])
            and pd.notna(latest["sma50"])
            and pd.notna(latest["close"])
            and prev["close"] <= prev["sma50"]
            and latest["close"] > latest["sma50"]
        ),
        "bearish_crossover_50": bool(
            pd.notna(prev["sma50"])
            and pd.notna(prev["close"])
            and pd.notna(latest["sma50"])
            and pd.notna(latest["close"])
            and prev["close"] >= prev["sma50"]
            and latest["close"] < latest["sma50"]
        ),
    }

    chart_rows = []
    for idx, row in view_df.iterrows():
        chart_rows.append(
            {
                "date": idx.date().isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "rsi14": float(row["rsi14"]) if pd.notna(row["rsi14"]) else None,
                "sma20": float(row["sma20"]) if pd.notna(row["sma20"]) else None,
                "sma50": float(row["sma50"]) if pd.notna(row["sma50"]) else None,
                "sma200": float(row["sma200"]) if pd.notna(row["sma200"]) else None,
                "macd": float(row["macd"]) if pd.notna(row["macd"]) else None,
                "macd_signal": float(row["macd_signal"]) if pd.notna(row["macd_signal"]) else None,
            }
        )

    return {
        "symbol": sym,
        "range": range.upper(),
        "updated_at": now_iso(),
        "provider": provider,
        "latest": {
            "close": float(latest["close"]),
            "rsi14": float(latest["rsi14"]) if pd.notna(latest["rsi14"]) else None,
            "sma20": float(latest["sma20"]) if pd.notna(latest["sma20"]) else None,
            "sma50": float(latest["sma50"]) if pd.notna(latest["sma50"]) else None,
            "sma200": float(latest["sma200"]) if pd.notna(latest["sma200"]) else None,
            "macd": float(latest["macd"]) if pd.notna(latest["macd"]) else None,
            "macd_signal": float(latest["macd_signal"]) if pd.notna(latest["macd_signal"]) else None,
            "rvol": float(latest["rvol"]) if pd.notna(latest["rvol"]) else None,
        },
        "flags": flags,
        "data": chart_rows,
        "calculation_notes": {
            "rsi14": "Wilder-style RSI(14) from close deltas.",
            "sma20": "20-day simple moving average.",
            "sma50": "50-day simple moving average.",
            "sma200": "200-day simple moving average.",
            "macd": "EMA(12)-EMA(26), signal line EMA(9) of MACD.",
        },
    }


@router.get("/api/ticker/{symbol}/quote")
def ticker_quote(symbol: str) -> dict[str, Any]:
    sym = validate_symbol(symbol)
    df = fetch_quote([sym], enrich=False)
    row = df[df["symbol"].astype(str).str.upper() == sym]
    if row.empty:
        raise HTTPException(status_code=404, detail=f"No quote found for {sym}")

    r = row.iloc[0]
    last_price = finite_or(r.get("last_price"), 0)
    prev_close = finite_or(r.get("prev_close"), 0)
    return {
        "symbol": sym,
        "name": str(r.get("name") or sym),
        "exchange": str(r.get("exchange") or ""),
        "last_price": last_price,
        "open": finite_or(r.get("open"), 0),
        "high": finite_or(r.get("high"), 0),
        "low": finite_or(r.get("low"), 0),
        "volume": finite_or(r.get("volume"), 0),
        "prev_close": prev_close,
        "year_high": finite_or(r.get("year_high"), 0),
        "year_low": finite_or(r.get("year_low"), 0),
        "ma_50d": finite_or(r.get("ma_50d"), 0),
        "ma_200d": finite_or(r.get("ma_200d"), 0),
        "daily_change_pct": normalize_pct((last_price / prev_close - 1) if prev_close else None),
        "provider": str(r.get("source_provider") or MARKET_DATA_PROVIDER),
        "updated_at": now_iso(),
    }
