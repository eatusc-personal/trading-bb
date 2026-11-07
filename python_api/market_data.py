"""Market data access: quote/history fetching through the provider router,
symbol validation, the default screener universe and quote-row helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from fastapi import HTTPException
from openbb import obb

from . import provider_budget
from .cache import get_cached, set_cached
from .config import (
    DISCOVERY_PROVIDER,
    HISTORY_CACHE_TTL_SEC,
    HISTORY_PROVIDER_PRIORITY,
    MARKET_DATA_PROVIDER,
    QUOTE_CACHE_TTL_SEC,
    QUOTE_PROVIDER_AUTH_COOLDOWN_SEC,
    QUOTE_PROVIDER_PRIORITY,
    SCREENER_HISTORY_WORKERS,
    SCREENER_UNIVERSE_SYMBOLS,
    SYMBOL_PATTERN,
)
from .providers import (
    dataframe_with_provider,
    enrich_market_cap_from_profile,
    fetch_history_from_provider,
    fetch_quote_from_provider,
    is_provider_enabled,
    is_provider_exhausted_or_unauthorized_error,
    normalize_history_df,
    provider_cooldown_remaining_sec,
    provider_record_failure,
    provider_record_success,
    resolve_runtime_priorities,
)
from .utils import finite_or, normalize_pct, to_float


def validate_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if not SYMBOL_PATTERN.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid symbol: {symbol}")
    return value


def market_cap_bucket(market_cap: float | None) -> str | None:
    if market_cap is None:
        return None
    if market_cap < 300_000_000:
        return "Micro"
    if market_cap < 2_000_000_000:
        return "Small"
    if market_cap < 10_000_000_000:
        return "Mid"
    if market_cap < 200_000_000_000:
        return "Large"
    return "Mega"


def row_to_market_record(row: pd.Series) -> dict[str, Any]:
    last_price = row.get("last_price")
    if last_price is None or pd.isna(last_price):
        last_price = row.get("price")

    prev_close = row.get("prev_close")
    if prev_close is None or pd.isna(prev_close) or float(prev_close) == 0:
        prev_close = row.get("open")

    daily_change_pct = None
    try:
        if prev_close is not None and not pd.isna(prev_close) and float(prev_close) != 0:
            daily_change_pct = (float(last_price) / float(prev_close) - 1) * 100
    except Exception:
        daily_change_pct = None

    market_cap = to_float(row.get("market_cap"))

    return {
        "symbol": str(row.get("symbol", "")),
        "name": str(row.get("name", "")),
        "price": finite_or(last_price, 0),
        "percent_change": normalize_pct(row.get("percent_change")) or daily_change_pct,
        "volume": finite_or(row.get("volume"), 0),
        "market_cap": market_cap,
        "market_cap_bucket": market_cap_bucket(market_cap),
    }


def range_to_start(range_key: str) -> str:
    key = range_key.upper()
    days_map = {"1M": 35, "3M": 100, "6M": 200, "1Y": 400, "5Y": 1900}
    days = days_map.get(key, 400)
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


def fetch_history(symbol: str, range_key: str = "1Y", ttl: int | None = None) -> pd.DataFrame:
    if ttl is None:
        ttl = HISTORY_CACHE_TTL_SEC
    cache_key = f"hist:{symbol}:{range_key}"
    cached = get_cached(cache_key, ttl)
    if cached is not None:
        return cached.copy()

    start_date = range_to_start(range_key)
    priorities = resolve_runtime_priorities(HISTORY_PROVIDER_PRIORITY, ("twelvedata", "fmp", "marketstack"))
    errors: list[str] = []
    for provider in priorities:
        if not is_provider_enabled(provider):
            continue
        if provider_cooldown_remaining_sec(provider) > 0:
            continue
        budget_ok, budget_reason = provider_budget.try_consume(provider, 1)
        if not budget_ok:
            errors.append(f"{provider}: {budget_reason}")
            continue
        try:
            raw = fetch_history_from_provider(provider, symbol, start_date)
            df = normalize_history_df(symbol, raw)
            provider_record_success(provider)
            df = dataframe_with_provider(df, provider)
            set_cached(cache_key, df)
            return df.copy()
        except HTTPException as exc:
            # Preserve client-side validation failures, but keep trying providers for
            # provider/data failures (empty history, temporary upstream errors, etc).
            if int(exc.status_code) == 400:
                raise
            errors.append(f"{provider}: {str(exc.detail)}")
            detail_l = str(exc.detail).lower()
            if int(exc.status_code) in {401, 402, 403, 429, 500, 502, 503, 504} or "invalid crumb" in detail_l:
                provider_record_failure(provider)
            continue
        except Exception as exc:
            errors.append(f"{provider}: {str(exc)}")
            if is_provider_exhausted_or_unauthorized_error(exc):
                provider_record_failure(
                    provider,
                    cooldown_sec=QUOTE_PROVIDER_AUTH_COOLDOWN_SEC if provider == "yfinance" else None,
                )
            continue
    detail = "; ".join(errors[:3]) if errors else "No enabled history providers."
    raise HTTPException(status_code=502, detail=f"Failed to fetch historical for {symbol}: {detail}")


def repair_quote_prices_from_history(df: pd.DataFrame) -> pd.DataFrame:
    """Fill rows whose quote lacks a usable last price from cached daily history.

    yfinance's quote endpoint returns NaN last_price for ETFs (only prev_close),
    which used to surface as bogus -100% moves. History is cached, so this costs
    at most one round-trip per broken symbol per HISTORY_CACHE_TTL_SEC.
    """
    if df.empty or "symbol" not in df.columns or "last_price" not in df.columns:
        return df

    broken: list[str] = []
    for _, row in df.iterrows():
        price = to_float(row.get("last_price"))
        if price is None or price <= 0:
            sym = str(row.get("symbol") or "").upper().strip()
            if sym:
                broken.append(sym)
    if not broken:
        return df
    broken = broken[:40]

    repairs: dict[str, tuple[float, float | None]] = {}
    with ThreadPoolExecutor(max_workers=max(2, min(6, SCREENER_HISTORY_WORKERS))) as pool:
        futures = {pool.submit(fetch_history, sym, "1M"): sym for sym in broken}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                hist = future.result()
                closes = hist["close"].dropna()
                if len(closes):
                    last = float(closes.iloc[-1])
                    prev = float(closes.iloc[-2]) if len(closes) > 1 else None
                    repairs[sym] = (last, prev)
            except Exception:
                continue

    if not repairs:
        return df
    out = df.copy()
    for idx, row in out.iterrows():
        sym = str(row.get("symbol") or "").upper().strip()
        repair = repairs.get(sym)
        if not repair:
            continue
        price = to_float(row.get("last_price"))
        if price is not None and price > 0:
            continue
        last, prev = repair
        out.at[idx, "last_price"] = last
        # Prefer history's own prior bar: when the quote has no last_price, its
        # "prev_close" is typically the latest close itself (=> bogus 0% moves).
        existing_prev = to_float(row.get("prev_close"))
        prev_close = prev if prev not in (None, 0) else existing_prev
        if prev_close not in (None, 0):
            out.at[idx, "prev_close"] = prev_close
            if "percent_change" in out.columns:
                out.at[idx, "percent_change"] = (last / prev_close - 1.0) * 100.0
    return out


def fetch_quote(symbols: list[str], ttl: int | None = None, enrich: bool = True) -> pd.DataFrame:
    """Fetch quotes via the provider router.

    enrich=False skips the market-cap profile enrichment (an extra yfinance
    round-trip): use it for callers that don't display/filter on market cap
    (ticker page, watchlist, screener, liveness checks).
    """
    if ttl is None:
        ttl = QUOTE_CACHE_TTL_SEC
    clean_symbols = sorted({str(s).upper().strip() for s in symbols if str(s).strip()})
    joined = ",".join(clean_symbols)
    cache_key = f"quote:{joined}:{int(bool(enrich))}"
    cached = get_cached(cache_key, ttl)
    if cached is not None:
        return cached.copy()

    priorities = resolve_runtime_priorities(
        QUOTE_PROVIDER_PRIORITY,
        ("twelvedata", "fmp", "marketstack"),
    )
    remaining = set(clean_symbols)
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    provider_used: list[str] = []

    for provider in priorities:
        if not remaining:
            break
        if not is_provider_enabled(provider):
            continue
        if provider_cooldown_remaining_sec(provider) > 0:
            continue
        chunk = sorted(remaining)
        # twelvedata bills one credit per symbol in a batch quote; others bill per call.
        budget_cost = len(chunk) if provider == "twelvedata" else 1
        budget_ok, budget_reason = provider_budget.try_consume(provider, budget_cost)
        if not budget_ok:
            errors.append(f"{provider}: {budget_reason}")
            continue
        try:
            provider_df = fetch_quote_from_provider(provider, chunk)
            if provider_df.empty or "symbol" not in provider_df.columns:
                provider_record_success(provider)
                continue
            provider_df["symbol"] = provider_df["symbol"].astype(str).str.upper().str.strip()
            provider_df = provider_df[provider_df["symbol"].isin(remaining)]
            if provider_df.empty:
                provider_record_success(provider)
                continue
            frames.append(provider_df)
            provider_used.append(provider)
            provider_record_success(provider)
            got = set(provider_df["symbol"].dropna().astype(str))
            remaining.difference_update(got)
        except Exception as exc:
            errors.append(f"{provider}: {str(exc)}")
            if is_provider_exhausted_or_unauthorized_error(exc):
                provider_record_failure(
                    provider,
                    cooldown_sec=QUOTE_PROVIDER_AUTH_COOLDOWN_SEC if provider == "yfinance" else None,
                )
            continue

    if not frames:
        detail = "; ".join(errors[:3]) if errors else "No enabled quote providers."
        raise HTTPException(status_code=502, detail=f"Failed to fetch quote(s): {detail}")

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["symbol"], keep="first")
    df = repair_quote_prices_from_history(df)
    if enrich:
        df = enrich_market_cap_from_profile(df)
    df = dataframe_with_provider(df, ",".join(provider_used) if provider_used else MARKET_DATA_PROVIDER)
    df.attrs["source_providers"] = provider_used
    if errors:
        df.attrs["provider_errors"] = errors[:3]
    if remaining:
        df.attrs["missing_symbols"] = sorted(remaining)
    set_cached(cache_key, df)
    return df.copy()


def build_universe() -> list[str]:
    cache_key = "universe:default"
    cached = get_cached(cache_key, 300)
    if cached is not None:
        return cached

    static_universe = [s for s in SCREENER_UNIVERSE_SYMBOLS if SYMBOL_PATTERN.match(s)]
    if static_universe:
        universe = sorted(set(static_universe))
        set_cached(cache_key, universe)
        return universe

    names = ["gainers", "losers", "active", "growth_tech", "aggressive_small_caps"]
    symbols: set[str] = set()

    for name in names:
        try:
            fn = getattr(obb.equity.discovery, name)
            data = fn(provider=DISCOVERY_PROVIDER) if DISCOVERY_PROVIDER else fn()
            df = data.to_df()
            if "symbol" in df.columns:
                for sym in df["symbol"].dropna().astype(str).head(40):
                    s = sym.upper().strip()
                    if SYMBOL_PATTERN.match(s):
                        symbols.add(s)
        except Exception:
            continue

    if not symbols:
        symbols = {"AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL"}

    universe = sorted(symbols)
    set_cached(cache_key, universe)
    return universe
