"""Market tab endpoints: overview, after-hours, indicators and news headlines.

The headlines endpoint lives here because the indicators builder consumes it."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query

from ..after_hours import _build_market_after_hours
from ..config import FMP_API_KEY, INDICATOR_DEFAULT_SYMBOLS, SYMBOL_PATTERN
from ..market_data import validate_symbol
from ..market_research import (
    _build_market_overview,
    fetch_fmp_earnings_calendar,
    fetch_fmp_grades,
    grade_to_score,
)
from ..news import _build_news_headlines
from ..social import classify_sentiment
from ..swr import swr_serve
from ..utils import now_iso, to_float

router = APIRouter()


@router.get("/api/market/overview")
def market_overview() -> dict[str, Any]:
    payload, meta = swr_serve("market:overview", _build_market_overview, refresh_after_sec=60)
    return {**payload, "cache": meta}


@router.get("/api/market/after-hours")
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


@router.get("/api/market/indicators")
def market_indicators(symbols: str | None = Query(default=None, description="Comma-separated symbols")) -> dict[str, Any]:
    symbols_param = symbols if isinstance(symbols, str) else None
    key = f"market:indicators:{symbols_param or 'default'}"
    payload, meta = swr_serve(key, lambda: _build_market_indicators(symbols_param), refresh_after_sec=300)
    return {**payload, "cache": meta}


@router.get("/api/news/headlines")
def news_headlines(limit: int = Query(default=30, ge=5, le=60)) -> dict[str, Any]:
    capped = max(5, min(60, int(limit)))
    payload, meta = swr_serve(f"news:headlines:{capped}", lambda: _build_news_headlines(capped), refresh_after_sec=300)
    return {**payload, "cache": meta}
