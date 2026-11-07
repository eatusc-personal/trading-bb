"""Market overview builder and FMP analyst-grade / earnings-calendar fetchers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from . import provider_budget
from .config import FMP_API_KEY, FMP_BASE, MARKET_DATA_PROVIDER
from .market_data import build_universe, fetch_quote, row_to_market_record
from .providers import provider_request_json
from .utils import now_iso


def _build_market_overview() -> dict[str, Any]:
    universe = build_universe()
    quotes = fetch_quote(universe)
    records: list[dict[str, Any]] = []
    for _, row in quotes.iterrows():
        rec = row_to_market_record(row)
        if rec.get("symbol"):
            records.append(rec)

    gainers = sorted(
        [r for r in records if r.get("percent_change") is not None],
        key=lambda x: float(x.get("percent_change") or 0),
        reverse=True,
    )[:20]
    losers = sorted(
        [r for r in records if r.get("percent_change") is not None],
        key=lambda x: float(x.get("percent_change") or 0),
    )[:20]
    active = sorted(
        records,
        key=lambda x: float(x.get("volume") or 0),
        reverse=True,
    )[:20]
    payload: dict[str, Any] = {
        "updated_at": now_iso(),
        "provider": MARKET_DATA_PROVIDER,
        "source_providers": quotes.attrs.get("source_providers", []),
        "gainers": gainers,
        "losers": losers,
        "active": active,
    }

    payload["summary"] = {
        "gainers_count": len(payload.get("gainers", [])),
        "losers_count": len(payload.get("losers", [])),
        "active_count": len(payload.get("active", [])),
    }
    return payload


def grade_to_score(grade: str | None) -> float | None:
    text = str(grade or "").strip().lower()
    if not text:
        return None
    if any(x in text for x in ["strong buy", "conviction buy"]):
        return 5.0
    if any(x in text for x in ["buy", "outperform", "overweight", "accumulate"]):
        return 4.0
    if any(x in text for x in ["hold", "neutral", "equal weight", "market perform", "sector perform"]):
        return 3.0
    if any(x in text for x in ["underperform", "underweight", "reduce"]):
        return 2.0
    if any(x in text for x in ["sell", "strong sell"]):
        return 1.0
    return None


def fetch_fmp_grades(symbol: str, limit: int = 80) -> list[dict[str, Any]]:
    if not FMP_API_KEY:
        return []
    budget_ok, _ = provider_budget.try_consume("fmp", 1)
    if not budget_ok:
        return []
    params = {"symbol": symbol, "limit": limit, "apikey": FMP_API_KEY}
    url = f"{FMP_BASE}/stable/grades?{urlencode(params)}"
    payload = provider_request_json(url)
    if isinstance(payload, str):
        return []
    if isinstance(payload, dict):
        msg = str(payload.get("Error Message") or payload.get("error") or "").lower()
        if msg:
            return []
        return []
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").upper().strip()
        if sym != symbol:
            continue
        out.append(item)
    return out


def fetch_fmp_earnings_calendar(date_from: str, date_to: str) -> list[dict[str, Any]]:
    if not FMP_API_KEY:
        return []
    budget_ok, _ = provider_budget.try_consume("fmp", 1)
    if not budget_ok:
        return []
    params = {
        "from": date_from,
        "to": date_to,
        "apikey": FMP_API_KEY,
    }
    url = f"{FMP_BASE}/stable/earnings-calendar?{urlencode(params)}"
    payload = provider_request_json(url)
    if isinstance(payload, str):
        return []
    if isinstance(payload, dict):
        msg = str(payload.get("Error Message") or payload.get("error") or "").lower()
        if msg:
            return []
        return []
    return payload if isinstance(payload, list) else []
