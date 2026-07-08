"""Market-data provider layer: quote/history fetchers for TwelveData, FMP,
Marketstack, and yfinance, plus the priority router, per-provider cooldowns,
and auth/quota backoff. Functions are moved verbatim from main.py."""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request as UrlRequest, urlopen

import numpy as np
import pandas as pd
from openbb import obb

from . import provider_budget
from .config import *  # noqa: F401,F403 -- provider keys, bases, timeouts, priorities
from .utils import call_openbb_silenced, finite_or, normalize_pct, to_float

logger = logging.getLogger("openbb_api.providers")

_provider_state_lock = threading.Lock()
_provider_cooldown_until: dict[str, float] = {}
_quote_provider_auth_cooldown_until = 0.0


def quote_provider_cooldown_remaining_sec() -> int:
    now = time.time()
    with _provider_state_lock:
        until = float(_quote_provider_auth_cooldown_until)
    if until <= now:
        return 0
    return int(max(0, round(until - now)))

def quote_provider_record_auth_failure() -> None:
    now = time.time()
    with _provider_state_lock:
        global _quote_provider_auth_cooldown_until
        _quote_provider_auth_cooldown_until = max(
            float(_quote_provider_auth_cooldown_until),
            now + max(30, QUOTE_PROVIDER_AUTH_COOLDOWN_SEC),
        )

def quote_provider_record_success() -> None:
    with _provider_state_lock:
        global _quote_provider_auth_cooldown_until
        _quote_provider_auth_cooldown_until = 0.0

def parse_provider_priority(raw: str, default: tuple[str, ...]) -> list[str]:
    allowed = {"twelvedata", "fmp", "marketstack", "yfinance"}
    parsed = [x.strip().lower() for x in str(raw or "").split(",") if x.strip()]
    unique: list[str] = []
    for provider in parsed:
        if provider in allowed and provider not in unique:
            unique.append(provider)
    if unique:
        return unique
    return list(default)

def resolve_runtime_priorities(raw: str, default: tuple[str, ...]) -> list[str]:
    priorities = parse_provider_priority(raw, default)
    has_enabled = any(is_provider_enabled(p) for p in priorities)
    if has_enabled:
        return priorities
    # Emergency fallback: avoid complete outage when no keyed providers are configured.
    if "yfinance" not in priorities:
        priorities.append("yfinance")
    return priorities

def is_provider_enabled(provider: str) -> bool:
    if provider == "twelvedata":
        return bool(TWELVE_DATA_API_KEY)
    if provider == "fmp":
        return bool(FMP_API_KEY)
    if provider == "marketstack":
        return bool(MARKETSTACK_API_KEY)
    if provider == "yfinance":
        return True
    return False

def provider_cooldown_remaining_sec(provider: str) -> int:
    now = time.time()
    with _provider_state_lock:
        until = float(_provider_cooldown_until.get(provider, 0.0))
    if until <= now:
        return 0
    return int(max(0, round(until - now)))

def provider_record_failure(provider: str, cooldown_sec: int | None = None) -> None:
    now = time.time()
    with _provider_state_lock:
        current = float(_provider_cooldown_until.get(provider, 0.0))
        _provider_cooldown_until[provider] = max(
            current,
            now + max(30, int(cooldown_sec or MARKET_DATA_PROVIDER_COOLDOWN_SEC)),
        )

def provider_record_success(provider: str) -> None:
    with _provider_state_lock:
        _provider_cooldown_until[provider] = 0.0

def is_provider_exhausted_or_unauthorized_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if isinstance(exc, HTTPError):
        if int(getattr(exc, "code", 0)) in {401, 402, 403, 429, 503}:
            return True
    tokens = [
        "rate limit",
        "too many requests",
        "quota",
        "api limit",
        "unauthorized",
        "invalid api key",
        "apikey invalid",
        "not available under your current subscription",
        "function_access_restricted",
        "restricted endpoint",
        "invalid crumb",
    ]
    return any(token in msg for token in tokens)

def provider_request_json(url: str, timeout: int | None = None) -> Any:
    return fetch_json_url(url, timeout=max(3, int(timeout or MARKET_DATA_TIMEOUT_SEC)))

def dataframe_with_provider(df: pd.DataFrame, provider: str) -> pd.DataFrame:
    out = df.copy()
    out.attrs["source_provider"] = provider
    return out

def is_quote_provider_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ("invalid crumb" in msg) or ("unauthorized" in msg) or is_provider_exhausted_or_unauthorized_error(exc)

def quote_from_twelvedata(symbols: list[str]) -> pd.DataFrame:
    params = {
        "symbol": ",".join(symbols),
        "apikey": TWELVE_DATA_API_KEY,
    }
    url = f"{TWELVE_DATA_BASE}/quote?{urlencode(params)}"
    payload = provider_request_json(url)
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise RuntimeError(str(payload.get("message") or payload))
    rows: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    if isinstance(payload, dict) and "symbol" in payload:
        entries = [payload]
    elif isinstance(payload, dict):
        entries = [v for v in payload.values() if isinstance(v, dict)]
    for item in entries:
        sym = str(item.get("symbol") or "").upper().strip()
        if not sym:
            continue
        fifty_two = item.get("fifty_two_week") if isinstance(item.get("fifty_two_week"), dict) else {}
        rows.append(
            {
                "symbol": sym,
                "name": str(item.get("name") or sym),
                "exchange": str(item.get("exchange") or ""),
                "last_price": to_float(item.get("close")) or to_float(item.get("last")) or 0.0,
                "open": to_float(item.get("open")) or 0.0,
                "high": to_float(item.get("high")) or 0.0,
                "low": to_float(item.get("low")) or 0.0,
                "volume": to_float(item.get("volume")) or 0.0,
                "prev_close": to_float(item.get("previous_close")) or 0.0,
                "year_high": to_float(fifty_two.get("high")) or 0.0,
                "year_low": to_float(fifty_two.get("low")) or 0.0,
                "ma_50d": None,
                "ma_200d": None,
                "percent_change": normalize_pct(item.get("percent_change")),
                "market_cap": None,
                "source_provider": "twelvedata",
            }
        )
    return pd.DataFrame(rows)

def quote_from_fmp(symbols: list[str]) -> pd.DataFrame:
    clean = [str(s).upper().strip() for s in symbols if str(s).strip()]
    if not clean:
        return pd.DataFrame()

    def fetch_single(sym: str) -> list[dict[str, Any]]:
        params = {"symbol": sym, "apikey": FMP_API_KEY}
        url = f"{FMP_BASE}/stable/quote?{urlencode(params)}"
        payload = provider_request_json(url)
        if isinstance(payload, str):
            raise RuntimeError(payload)
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(str(payload.get("error")))
        return payload if isinstance(payload, list) else []

    items: list[dict[str, Any]] = []
    if len(clean) == 1:
        items = fetch_single(clean[0])
    else:
        # FMP supports multi-symbol quote via batch endpoint.
        params = {"symbols": ",".join(clean), "apikey": FMP_API_KEY}
        url = f"{FMP_BASE}/stable/batch-quote?{urlencode(params)}"
        try:
            payload = provider_request_json(url)
            if isinstance(payload, str):
                raise RuntimeError(payload)
            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError(str(payload.get("error")))
            items = payload if isinstance(payload, list) else []
        except Exception:
            # Some plans may block batch quote; degrade gracefully to per-symbol calls.
            for sym in clean:
                try:
                    items.extend(fetch_single(sym))
                except Exception:
                    continue

    rows: list[dict[str, Any]] = []
    for item in items:
        symbol = str(item.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": str(item.get("name") or symbol),
                "exchange": str(item.get("exchange") or ""),
                "last_price": to_float(item.get("price")) or 0.0,
                "open": to_float(item.get("open")) or 0.0,
                "high": to_float(item.get("dayHigh")) or 0.0,
                "low": to_float(item.get("dayLow")) or 0.0,
                "volume": to_float(item.get("volume")) or 0.0,
                "prev_close": to_float(item.get("previousClose")) or 0.0,
                "year_high": to_float(item.get("yearHigh")) or 0.0,
                "year_low": to_float(item.get("yearLow")) or 0.0,
                "ma_50d": to_float(item.get("priceAvg50")),
                "ma_200d": to_float(item.get("priceAvg200")),
                "percent_change": normalize_pct(item.get("changePercentage")),
                "market_cap": to_float(item.get("marketCap")),
                "source_provider": "fmp",
            }
        )
    return pd.DataFrame(rows)

def quote_from_marketstack(symbols: list[str]) -> pd.DataFrame:
    params = {
        "access_key": MARKETSTACK_API_KEY,
        "symbols": ",".join(symbols),
        "limit": 1000,
        "sort": "DESC",
    }
    url = f"{MARKETSTACK_BASE}/eod?{urlencode(params)}"
    payload = provider_request_json(url)
    if isinstance(payload, dict) and payload.get("error"):
        err = payload.get("error") or {}
        raise RuntimeError(str(err.get("message") or err.get("code") or err))
    data = payload.get("data") if isinstance(payload, dict) else []
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for item in data if isinstance(data, list) else []:
        sym = str(item.get("symbol") or "").upper().strip()
        if not sym:
            continue
        by_symbol.setdefault(sym, []).append(item)
    rows: list[dict[str, Any]] = []
    for sym, items in by_symbol.items():
        item = items[0]
        latest_close = to_float(item.get("close"))
        prev_close = to_float(items[1].get("close")) if len(items) > 1 else None
        pct = ((latest_close / prev_close - 1) * 100) if latest_close is not None and prev_close not in (None, 0) else None
        rows.append(
            {
                "symbol": sym,
                "name": sym,
                "exchange": str(item.get("exchange") or ""),
                "last_price": latest_close or 0.0,
                "open": to_float(item.get("open")) or 0.0,
                "high": to_float(item.get("high")) or 0.0,
                "low": to_float(item.get("low")) or 0.0,
                "volume": to_float(item.get("volume")) or 0.0,
                "prev_close": prev_close,
                "year_high": None,
                "year_low": None,
                "ma_50d": None,
                "ma_200d": None,
                "percent_change": normalize_pct(pct),
                "market_cap": None,
                "source_provider": "marketstack",
            }
        )
    return pd.DataFrame(rows)

def quote_from_yfinance(symbols: list[str]) -> pd.DataFrame:
    data = call_openbb_silenced(obb.equity.price.quote, symbol=symbols, provider=DEFAULT_PROVIDER)
    df = data.to_df().copy()
    if "symbol" not in df.columns:
        return pd.DataFrame()
    df["source_provider"] = DEFAULT_PROVIDER
    if "market_cap" not in df.columns:
        df["market_cap"] = None
    return df

def enrich_market_cap_from_profile(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "symbol" not in df.columns:
        return df

    out = df.copy()
    if "market_cap" not in out.columns:
        out["market_cap"] = None

    missing_symbols = sorted(
        {
            str(sym).upper().strip()
            for sym, mc in zip(out["symbol"], out["market_cap"], strict=False)
            if str(sym).strip() and to_float(mc) is None
        }
    )
    if not missing_symbols:
        return out

    market_caps: dict[str, float] = {}
    symbols_to_fetch: list[str] = []

    for sym in missing_symbols:
        cached = get_cached(f"mcap:{sym}", 6 * 3600)
        cached_value = to_float(cached) if cached is not None else None
        if cached_value is not None:
            market_caps[sym] = cached_value
        else:
            symbols_to_fetch.append(sym)

    if symbols_to_fetch:
        for i in range(0, len(symbols_to_fetch), 25):
            chunk = symbols_to_fetch[i : i + 25]
            budget_ok, _budget_reason = provider_budget.try_consume("yfinance", 1)
            if not budget_ok:
                break
            try:
                profile = call_openbb_silenced(obb.equity.profile, symbol=chunk, provider="yfinance")
                profile_df = profile.to_df().copy()
                if "symbol" not in profile_df.columns or "market_cap" not in profile_df.columns:
                    continue
                profile_df["symbol"] = profile_df["symbol"].astype(str).str.upper().str.strip()
                for _, row in profile_df.iterrows():
                    sym = str(row.get("symbol") or "").upper().strip()
                    if not sym:
                        continue
                    cap = to_float(row.get("market_cap"))
                    if cap is None:
                        continue
                    market_caps[sym] = cap
                    set_cached(f"mcap:{sym}", cap)
            except Exception as exc:
                logger.debug("Market-cap profile enrichment failed for %s: %s", ",".join(chunk), exc)
                continue

    if not market_caps:
        return out

    out["symbol"] = out["symbol"].astype(str).str.upper().str.strip()
    out["market_cap"] = out.apply(
        lambda row: market_caps.get(str(row.get("symbol") or "").upper().strip(), to_float(row.get("market_cap"))),
        axis=1,
    )
    return out

def fetch_quote_from_provider(provider: str, symbols: list[str]) -> pd.DataFrame:
    if provider == "twelvedata":
        return quote_from_twelvedata(symbols)
    if provider == "fmp":
        return quote_from_fmp(symbols)
    if provider == "marketstack":
        return quote_from_marketstack(symbols)
    if provider == "yfinance":
        return quote_from_yfinance(symbols)
    raise RuntimeError(f"Unsupported quote provider: {provider}")

def history_from_twelvedata(symbol: str, start_date: str) -> pd.DataFrame:
    start_dt = pd.to_datetime(start_date)
    days = max(40, int((datetime.now(UTC).date() - start_dt.date()).days + 20))
    params = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": min(5000, days),
        "apikey": TWELVE_DATA_API_KEY,
    }
    url = f"{TWELVE_DATA_BASE}/time_series?{urlencode(params)}"
    payload = provider_request_json(url)
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise RuntimeError(str(payload.get("message") or payload))
    values = payload.get("values") if isinstance(payload, dict) else []
    rows: list[dict[str, Any]] = []
    for item in values if isinstance(values, list) else []:
        date_str = str(item.get("datetime") or "").strip()
        if not date_str:
            continue
        rows.append(
            {
                "date": date_str[:10],
                "open": to_float(item.get("open")) or 0.0,
                "high": to_float(item.get("high")) or 0.0,
                "low": to_float(item.get("low")) or 0.0,
                "close": to_float(item.get("close")) or 0.0,
                "volume": to_float(item.get("volume")) or 0.0,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[df["date"] >= start_dt]
    return df

def history_from_fmp(symbol: str, start_date: str) -> pd.DataFrame:
    params = {
        "symbol": symbol,
        "from": start_date,
        "to": datetime.now(UTC).date().isoformat(),
        "apikey": FMP_API_KEY,
    }
    url = f"{FMP_BASE}/stable/historical-price-eod/full?{urlencode(params)}"
    payload = provider_request_json(url)
    if isinstance(payload, str):
        raise RuntimeError(payload)
    if isinstance(payload, dict) and payload.get("Error Message"):
        raise RuntimeError(str(payload.get("Error Message")))
    rows = payload if isinstance(payload, list) else []
    df = pd.DataFrame(rows)
    return df

def history_from_marketstack(symbol: str, start_date: str) -> pd.DataFrame:
    params = {
        "access_key": MARKETSTACK_API_KEY,
        "symbols": symbol,
        "date_from": start_date,
        "date_to": datetime.now(UTC).date().isoformat(),
        "limit": 1000,
        "sort": "ASC",
    }
    url = f"{MARKETSTACK_BASE}/eod?{urlencode(params)}"
    payload = provider_request_json(url)
    if isinstance(payload, dict) and payload.get("error"):
        err = payload.get("error") or {}
        raise RuntimeError(str(err.get("message") or err.get("code") or err))
    rows = payload.get("data") if isinstance(payload, dict) else []
    df = pd.DataFrame(rows if isinstance(rows, list) else [])
    return df

def history_from_yfinance(symbol: str, start_date: str) -> pd.DataFrame:
    data = call_openbb_silenced(
        obb.equity.price.historical,
        symbol=symbol,
        start_date=start_date,
        provider=DEFAULT_PROVIDER,
    )
    return data.to_df().copy()

def fetch_history_from_provider(provider: str, symbol: str, start_date: str) -> pd.DataFrame:
    if provider == "twelvedata":
        return history_from_twelvedata(symbol, start_date)
    if provider == "fmp":
        return history_from_fmp(symbol, start_date)
    if provider == "marketstack":
        return history_from_marketstack(symbol, start_date)
    if provider == "yfinance":
        return history_from_yfinance(symbol, start_date)
    raise RuntimeError(f"Unsupported history provider: {provider}")

def normalize_history_df(symbol: str, raw_df: pd.DataFrame) -> pd.DataFrame:
    df = raw_df.copy()
    if df.empty:
        raise HTTPException(status_code=404, detail=f"No historical data for {symbol}")

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[df["date"].notna()].sort_values("date").set_index("date")
    else:
        df.index = pd.to_datetime(df.index, errors="coerce")
        df = df[df.index.notna()].sort_index()

    required = ["open", "high", "low", "close", "volume"]
    for col in required:
        if col not in df.columns:
            raise HTTPException(status_code=502, detail=f"Historical data missing '{col}' for {symbol}")
    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=required)
    if df.empty:
        raise HTTPException(status_code=404, detail=f"No historical data for {symbol}")
    return df
