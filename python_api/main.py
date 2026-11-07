from __future__ import annotations

import logging
import re
import threading
import time
import random
import json
import math
import os
import sqlite3
import io
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request as UrlRequest, urlopen
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openbb import obb
from pydantic import BaseModel, Field

from . import provider_budget
from .indicators import (
    compute_indicators,
    compute_technical_trigger_flags,
    trigger_labels,
)

logging.basicConfig(
    level=os.getenv("API_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("openbb_api")


from .config import *  # noqa: F401,F403 -- env constants + static data tables
from .cache import get_cached, set_cached
from .db import _social_db_conn, _social_db_lock
from .symbol_master import (
    refresh_symbol_alias_seed,
    symbol_alias_health,
    load_symbol_master,
    symbol_master_health,
    symbol_master_snapshots,
    normalize_as_of_date,
    load_symbol_master_at_date,
    resolve_symbol_alias,
    resolve_symbol_validity,
)
from .news import _build_news_headlines
from .social import (
    mention_context_score,
    extract_tickers,
    classify_sentiment,
    weighted_source_signal,
    compute_mention_quality,
    confidence_tier_from_score,
    extract_catalyst_hits,
    options_context_hits,
    parse_reddit_listing,
    parse_old_reddit,
    fetch_apewisdom_wallstreetbets,
    fetch_stocktwits_trending,
    fetch_stocktwits_messages,
    fetch_tiktok_messages,
)
from .net import (
    fetch_json_url,
    fetch_text_url,
    reddit_rate_wait,
    source_cooldown_remaining_sec,
    source_record_success,
    source_record_failure,
)
from .utils import call_openbb_silenced, finite_or, normalize_pct, now_iso, to_float
from .providers import (
    quote_provider_cooldown_remaining_sec,
    quote_provider_record_auth_failure,
    quote_provider_record_success,
    parse_provider_priority,
    resolve_runtime_priorities,
    is_provider_enabled,
    provider_cooldown_remaining_sec,
    provider_record_failure,
    provider_record_success,
    is_provider_exhausted_or_unauthorized_error,
    provider_request_json,
    dataframe_with_provider,
    is_quote_provider_auth_error,
    quote_from_twelvedata,
    quote_from_fmp,
    quote_from_marketstack,
    quote_from_yfinance,
    enrich_market_cap_from_profile,
    fetch_quote_from_provider,
    history_from_twelvedata,
    history_from_fmp,
    history_from_marketstack,
    history_from_yfinance,
    fetch_history_from_provider,
    normalize_history_df,
)
from .after_hours import (
    _build_market_after_hours,
)
from .etfs import (
    get_popular_etf_snapshot,
)
from .market_data import (
    build_universe,
    fetch_history,
    fetch_quote,
    range_to_start,
    validate_symbol,
)
from .market_research import (
    _build_market_overview,
    fetch_fmp_earnings_calendar,
    fetch_fmp_grades,
    grade_to_score,
)
from .retention import (
    _schedule_daily_prune,
    prune_sqlite_caches,
)
from .security import (
    client_ip,
    enforce_social_api_key,
    rate_limit_check,
)
from .social_refresh import (
    SOCIAL_RESULT_REFRESH_AFTER_SEC,
    _social_result_lock,
    _social_result_state,
    ensure_social_refresh,
)
from .swr import (
    swr_serve,
)

app = FastAPI(title=APP_NAME, version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS or ["http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def api_rate_limit_middleware(request: FastAPIRequest, call_next):
    path = str(request.url.path or "")
    if not path.startswith("/api/"):
        return await call_next(request)
    if path in {"/api/health"}:
        return await call_next(request)

    subject = client_ip(request)
    ok_api, api_detail = rate_limit_check(
        "api",
        subject,
        max(10, API_RATE_LIMIT_MAX_REQUESTS),
        max(1, API_RATE_LIMIT_WINDOW_SEC),
    )
    if not ok_api:
        return JSONResponse(
            {"error": "rate_limit_exceeded", "scope": "api", "retry_after_sec": api_detail},
            status_code=429,
            headers={"Retry-After": str(api_detail)},
        )

    if path.startswith("/api/social/"):
        ok_social, social_detail = rate_limit_check(
            "social",
            subject,
            max(5, SOCIAL_RATE_LIMIT_MAX_REQUESTS),
            max(1, SOCIAL_RATE_LIMIT_WINDOW_SEC),
        )
        if not ok_social:
            return JSONResponse(
                {"error": "rate_limit_exceeded", "scope": "social", "retry_after_sec": social_detail},
                status_code=429,
                headers={"Retry-After": str(social_detail)},
            )

    return await call_next(request)


@app.on_event("startup")
def _startup_retention() -> None:
    try:
        prune_sqlite_caches()
    except Exception as exc:
        logger.warning("SQLite retention pass failed at startup: %s", exc)
    _schedule_daily_prune()


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


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": now_iso(), "provider": MARKET_DATA_PROVIDER}


@app.get("/api/security/status")
def security_status() -> dict[str, Any]:
    return {
        "updated_at": now_iso(),
        "cors_allow_origins": CORS_ALLOW_ORIGINS,
        "social_api_key_configured": bool(SOCIAL_API_KEY),
        "market_data": {
            "provider": MARKET_DATA_PROVIDER,
            "keys_configured": {
                "twelvedata": bool(TWELVE_DATA_API_KEY),
                "fmp": bool(FMP_API_KEY),
                "marketstack": bool(MARKETSTACK_API_KEY),
                "yfinance": ENABLE_YFINANCE,
            },
            "quote_priority": resolve_runtime_priorities(QUOTE_PROVIDER_PRIORITY, ("twelvedata", "fmp", "marketstack")),
            "history_priority": resolve_runtime_priorities(HISTORY_PROVIDER_PRIORITY, ("twelvedata", "fmp", "marketstack")),
        },
        "rate_limit": {
            "api": {"window_sec": API_RATE_LIMIT_WINDOW_SEC, "max_requests": API_RATE_LIMIT_MAX_REQUESTS},
            "social": {"window_sec": SOCIAL_RATE_LIMIT_WINDOW_SEC, "max_requests": SOCIAL_RATE_LIMIT_MAX_REQUESTS},
        },
        "provider_budget": provider_budget.snapshot(),
    }


@app.get("/api/providers/budget")
def providers_budget() -> dict[str, Any]:
    return {"updated_at": now_iso(), **provider_budget.snapshot()}


@app.get("/api/social/symbol-master/health")
def social_symbol_master_health(request: FastAPIRequest) -> dict[str, Any]:
    enforce_social_api_key(request)
    return symbol_master_health()


@app.get("/api/social/symbol-master/snapshots")
def social_symbol_master_snapshots(request: FastAPIRequest, limit: int = Query(default=60, ge=1, le=365)) -> dict[str, Any]:
    enforce_social_api_key(request)
    return symbol_master_snapshots(limit)


@app.get("/api/social/symbol-master/alias/health")
def social_symbol_alias_health(request: FastAPIRequest) -> dict[str, Any]:
    enforce_social_api_key(request)
    return symbol_alias_health()


@app.post("/api/social/symbol-master/alias/refresh")
def social_symbol_alias_refresh(request: FastAPIRequest) -> dict[str, Any]:
    enforce_social_api_key(request)
    result = refresh_symbol_alias_seed(force=True)
    return {"updated_at": now_iso(), **result}


@app.get("/api/social/symbol-master/resolve")
def social_symbol_master_resolve(
    request: FastAPIRequest,
    symbol: str = Query(..., min_length=1, max_length=12),
    as_of: str | None = Query(default=None, description="ISO date (YYYY-MM-DD). Defaults to today."),
) -> dict[str, Any]:
    enforce_social_api_key(request)
    normalized = validate_symbol(symbol)
    as_of_date = normalize_as_of_date(as_of)
    validity = resolve_symbol_validity(normalized, as_of_date)
    # Also return snapshot-level context for reproducibility.
    symbol_master, snapshot_date, snapshot_source = load_symbol_master_at_date(as_of_date)
    return {
        "updated_at": now_iso(),
        "input_symbol": normalized,
        "as_of_date": as_of_date,
        "snapshot_date": snapshot_date,
        "snapshot_source": snapshot_source,
        "symbol_master_count": len(symbol_master),
        "resolution": validity,
    }


@app.get("/api/market/overview")
def market_overview() -> dict[str, Any]:
    payload, meta = swr_serve("market:overview", _build_market_overview, refresh_after_sec=60)
    return {**payload, "cache": meta}


@app.get("/api/market/after-hours")
def market_after_hours(refresh: bool = Query(default=False)) -> dict[str, Any]:
    payload, meta = swr_serve(
        "market:after_hours", _build_market_after_hours, refresh_after_sec=120, force_refresh=refresh
    )
    return {**payload, "cache": meta}


def _build_market_indicators(symbols_param: str | None) -> dict[str, Any]:
    if symbols_param:
        watch_symbols = [validate_symbol(s) for s in symbols_param.split(",") if s.strip()][:25]
    else:
        watch_symbols = [s for s in INDICATOR_DEFAULT_SYMBOLS if SYMBOL_PATTERN.match(s)][:25]
    if not watch_symbols:
        watch_symbols = ["AAPL", "MSFT", "NVDA", "SPY"]

    # Headline indicators from existing RSS/news pipeline.
    headlines = news_headlines(limit=50)
    rows = headlines.get("rows", []) if isinstance(headlines, dict) else []
    now = datetime.now(UTC)
    sentiment_scores: list[float] = []
    last_24h = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        summary = str(row.get("summary") or "")
        sent = classify_sentiment(f"{title}\n{summary}")
        sentiment_scores.append(1.0 if sent == "bullish" else -1.0 if sent == "bearish" else 0.0)
        pub = str(row.get("published_at") or "")
        try:
            published_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if (now - published_dt).total_seconds() <= 24 * 3600:
                last_24h += 1
        except Exception:
            continue
    headline_sentiment_score = round((sum(sentiment_scores) / len(sentiment_scores)) if sentiment_scores else 0.0, 4)

    # Analyst grade drift indicators from FMP grades (fetched in parallel).
    grade_deltas: list[float] = []
    analyst_updates: list[dict[str, Any]] = []
    grades_by_symbol: dict[str, list[dict[str, Any]]] = {}
    if FMP_API_KEY and watch_symbols:
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(watch_symbols)))) as grade_pool:
            grade_futures = {grade_pool.submit(fetch_fmp_grades, sym, 60): sym for sym in watch_symbols}
            for grade_future in as_completed(grade_futures):
                try:
                    grades_by_symbol[grade_futures[grade_future]] = grade_future.result()
                except Exception:
                    grades_by_symbol[grade_futures[grade_future]] = []
    for sym in watch_symbols:
        grades = grades_by_symbol.get(sym, [])
        for g in grades[:20]:
            new_grade = str(g.get("newGrade") or "")
            prev_grade = str(g.get("previousGrade") or "")
            new_score = grade_to_score(new_grade)
            prev_score = grade_to_score(prev_grade)
            if new_score is not None and prev_score is not None:
                grade_deltas.append(new_score - prev_score)
            analyst_updates.append(
                {
                    "symbol": sym,
                    "date": str(g.get("date") or ""),
                    "company": str(g.get("gradingCompany") or ""),
                    "action": str(g.get("action") or "").lower(),
                    "new_grade": new_grade,
                    "previous_grade": prev_grade,
                }
            )
    analyst_updates.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    analyst_grade_drift = round((sum(grade_deltas) / len(grade_deltas)) if grade_deltas else 0.0, 4)

    # Earnings proximity from FMP earnings calendar.
    today = datetime.now(UTC).date()
    earnings_rows: list[dict[str, Any]] = []
    earnings_soon: list[dict[str, Any]] = []
    date_from = today.isoformat()
    date_to = (today + timedelta(days=30)).isoformat()
    try:
        cal = fetch_fmp_earnings_calendar(date_from, date_to)
    except Exception:
        cal = []
    watch_set = set(watch_symbols)
    for item in cal:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").upper().strip()
        if not sym or sym not in watch_set:
            continue
        d = str(item.get("date") or "")
        if not d:
            continue
        try:
            event_date = datetime.fromisoformat(d).date()
        except Exception:
            continue
        days = (event_date - today).days
        row = {
            "symbol": sym,
            "date": d,
            "days_until": days,
            "eps_estimated": to_float(item.get("epsEstimated")),
            "revenue_estimated": to_float(item.get("revenueEstimated")),
        }
        earnings_rows.append(row)
        if 0 <= days <= 7:
            earnings_soon.append(row)
    earnings_rows.sort(key=lambda x: int(x.get("days_until") or 9999))
    earnings_soon.sort(key=lambda x: int(x.get("days_until") or 9999))

    payload: dict[str, Any] = {
        "updated_at": now_iso(),
        "provider": "composite",
        "watch_symbols": watch_symbols,
        "headline": {
            "sentiment_score": headline_sentiment_score,
            "news_velocity_24h": int(last_24h),
            "sample_size": len(rows),
            "source": "rss_blend",
        },
        "analyst": {
            "grade_drift_score": analyst_grade_drift,
            "update_count": len(analyst_updates),
            "recent_updates": analyst_updates[:20],
            "source": "fmp_grades",
        },
        "earnings": {
            "next_7d_count": len(earnings_soon),
            "next_events": earnings_rows[:20],
            "source": "fmp_earnings_calendar",
        },
        "notes": {
            "headline_indicator": "Derived from aggregated headline feed using rule-based sentiment.",
            "social_sentiment": "Use /api/social/sentiment for social-source sentiment signals.",
            "plan_limits": "Some FMP news endpoints may be restricted by subscription tier.",
        },
    }
    return payload


@app.get("/api/market/indicators")
def market_indicators(symbols: str | None = Query(default=None, description="Comma-separated symbols")) -> dict[str, Any]:
    symbols_param = symbols if isinstance(symbols, str) else None
    key = f"market:indicators:{symbols_param or 'default'}"
    payload, meta = swr_serve(key, lambda: _build_market_indicators(symbols_param), refresh_after_sec=300)
    return {**payload, "cache": meta}


@app.get("/api/news/headlines")
def news_headlines(limit: int = Query(default=30, ge=5, le=60)) -> dict[str, Any]:
    capped = max(5, min(60, int(limit)))
    payload, meta = swr_serve(f"news:headlines:{capped}", lambda: _build_news_headlines(capped), refresh_after_sec=300)
    return {**payload, "cache": meta}


@app.get("/api/ticker/{symbol}/historical")
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


@app.get("/api/ticker/{symbol}/technicals")
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


@app.get("/api/ticker/{symbol}/quote")
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


@app.post("/api/screener")
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


@app.get("/api/screener/top-volume-triggers")
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


@app.get("/api/social/sentiment")
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


@app.get("/api/social/signal-lab/runs")
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


@app.get("/api/social/signal-lab/run/{run_id}")
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


@app.get("/api/social/signal-lab/event-study/{run_id}")
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


@app.get("/api/etfs/popular")
def popular_etfs(
    limit: int = Query(default=ETF_POPULAR_LIMIT, ge=1, le=500),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    return get_popular_etf_snapshot(limit=limit, refresh=refresh)


@app.get("/api/watchlist/quote")
def watchlist_quote(symbols: str = Query(..., description="Comma-separated symbols")) -> dict[str, Any]:
    parsed = [validate_symbol(s) for s in symbols.split(",") if s.strip()]
    if not parsed:
        raise HTTPException(status_code=400, detail="No valid symbols provided")

    df = fetch_quote(parsed, enrich=False)
    rows = []
    for _, r in df.iterrows():
        symbol = str(r.get("symbol") or "").upper()
        prev_close = finite_or(r.get("prev_close"), 0)
        last_price = finite_or(r.get("last_price"), 0)
        change_pct = ((last_price - prev_close) / prev_close * 100) if prev_close else None
        rows.append(
            {
                "symbol": symbol,
                "name": str(r.get("name") or symbol),
                "price": finite_or(last_price, 0),
                "change_pct": change_pct,
                "volume": finite_or(r.get("volume"), 0),
                "provider": str(r.get("source_provider") or ""),
            }
        )

    return {
        "quotes": rows,
        "count": len(rows),
        "updated_at": now_iso(),
        "provider": MARKET_DATA_PROVIDER,
        "source_providers": df.attrs.get("source_providers", []),
    }


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


@app.get("/api/alerts")
def alerts(symbols: str | None = Query(default=None)) -> dict[str, Any]:
    if symbols:
        # Explicit symbol lists compute live — histories are cached, so this is fast.
        return _build_alerts(symbols)
    payload, meta = swr_serve("alerts:default", lambda: _build_alerts(None), refresh_after_sec=120)
    return {**payload, "cache": meta}


# ---------------------------------------------------------------------------
# Divergence scanner (peer-group outlier detection) — see python_api/divergence.py
# ---------------------------------------------------------------------------
from .divergence import router as divergence_router  # noqa: E402

app.include_router(divergence_router)

# ---------------------------------------------------------------------------
# Daily cross-tab report (confluence-ranked opportunities) — python_api/report.py
# ---------------------------------------------------------------------------
from .report import router as report_router  # noqa: E402

app.include_router(report_router)
