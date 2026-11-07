"""Configuration and environment constants for the Trading Dashboard BB API.

All env-derived settings and the static data tables (ticker exclude lists,
sentiment lexicons, catalyst keywords, provider bases, cache TTLs, rate
limits, ...) live here as one source of truth. .env files load at import.
"""

from __future__ import annotations

import logging
import os
import re

_logger = logging.getLogger("openbb_api.config")


def load_local_env_files() -> None:
    base_dir = os.path.dirname(__file__)
    candidates = (
        os.path.join(base_dir, ".env"),
        os.path.join(base_dir, ".env.local"),
        os.path.join(base_dir, "..", ".env"),
        os.path.join(base_dir, "..", ".env.local"),
    )
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    if not key or key in os.environ:
                        continue
                    value = value.strip().strip('"').strip("'")
                    os.environ[key] = value
        except Exception as exc:
            _logger.warning("Failed to load env file %s: %s", path, exc)
            continue


load_local_env_files()

APP_NAME = "Trading Dashboard BB API"
DEFAULT_PROVIDER = "yfinance"
MARKET_DATA_PROVIDER = "router"
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
MARKETSTACK_API_KEY = os.getenv("MARKETSTACK_API_KEY", "").strip()
FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()
TWELVE_DATA_BASE = os.getenv("TWELVE_DATA_BASE", "https://api.twelvedata.com").rstrip("/")
MARKETSTACK_BASE = os.getenv("MARKETSTACK_BASE", "https://api.marketstack.com/v1").rstrip("/")
FMP_BASE = os.getenv("FMP_BASE", "https://financialmodelingprep.com").rstrip("/")
MARKET_DATA_TIMEOUT_SEC = int(os.getenv("MARKET_DATA_TIMEOUT_SEC", "8"))
MARKET_DATA_PROVIDER_COOLDOWN_SEC = int(os.getenv("MARKET_DATA_PROVIDER_COOLDOWN_SEC", "180"))
ENABLE_YFINANCE = os.getenv("ENABLE_YFINANCE", "0").strip().lower() not in {"0", "false", "off", "no"}
DISCOVERY_PROVIDER = os.getenv("DISCOVERY_PROVIDER", DEFAULT_PROVIDER).strip().lower()
if DISCOVERY_PROVIDER == "yfinance" and not ENABLE_YFINANCE:
    DISCOVERY_PROVIDER = ""
QUOTE_PROVIDER_PRIORITY = os.getenv("QUOTE_PROVIDER_PRIORITY", "twelvedata,fmp,marketstack")
HISTORY_PROVIDER_PRIORITY = os.getenv("HISTORY_PROVIDER_PRIORITY", "twelvedata,fmp,marketstack")
SCREENER_UNIVERSE_SYMBOLS = [
    x.strip().upper()
    for x in os.getenv(
        "SCREENER_UNIVERSE_SYMBOLS",
        "AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL,SPY,QQQ,AMD,PLTR,SMCI,AVGO,COST,JPM,BA,XOM,UNH,JNJ,WMT,ORCL,CRM,DIS,PFE,INTC,COIN,SOFI,RIVN,NIO,SHOP,UBER,NFLX,ADBE,PYPL,SQ,HOOD,BABA,TSM,ASML,MRNA,KOSS,GPRO,CHPT,SPCE,FUBO,AMC,CLOV,KSS,PTON,BB,UAA,UPST,LUNR,SOUN,PLUG,MARA,ACHR",
    ).split(",")
    if x.strip()
]
INDICATOR_DEFAULT_SYMBOLS = [
    x.strip().upper()
    for x in os.getenv(
        "INDICATOR_DEFAULT_SYMBOLS",
        "AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL,SPY,QQQ",
    ).split(",")
    if x.strip()
]
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")
TICKER_PATTERN = re.compile(r"\$?[A-Za-z]{2,5}\b")
SOCIAL_SUBREDDITS = ["wallstreetbets", "stocks", "options", "investing", "shortsqueeze"]
# Identify the app honestly instead of impersonating a browser. Public data
# aggregators should say who they are; spoofing a Chrome UA is both a
# compliance smell and easy for a source to fingerprint anyway.
HONEST_USER_AGENT = "trading-bb/1.0 (+https://github.com/eatusc-personal/trading-bb; educational research use)"
SOCIAL_USER_AGENTS = [HONEST_USER_AGENT]
SOCIAL_REDDIT_USER_AGENT = os.getenv(
    "REDDIT_USER_AGENT",
    "openbb-local-dashboard/1.0 (by /u/openbb_local_scanner)",
)
REDDIT_MIN_INTERVAL_SEC = float(os.getenv("REDDIT_MIN_INTERVAL_SEC", "0.8"))
REDDIT_PUBLIC_LIMIT = int(os.getenv("REDDIT_PUBLIC_LIMIT", "25"))
REDDIT_FETCH_RETRIES = int(os.getenv("REDDIT_FETCH_RETRIES", "1"))
REDDIT_BACKOFF_BASE_SEC = float(os.getenv("REDDIT_BACKOFF_BASE_SEC", "0.4"))
REDDIT_FETCH_TIMEOUT_SEC = int(os.getenv("REDDIT_FETCH_TIMEOUT_SEC", "2"))
TIKTOK_PROVIDER_URL = os.getenv("TIKTOK_PROVIDER_URL", "").strip()
TIKTOK_PROVIDER_API_KEY = os.getenv("TIKTOK_PROVIDER_API_KEY", "").strip()
TIKTOK_PROVIDER_TIMEOUT_SEC = int(os.getenv("TIKTOK_PROVIDER_TIMEOUT_SEC", "10"))
STOCKTWITS_FETCH_TIMEOUT_SEC = int(os.getenv("STOCKTWITS_FETCH_TIMEOUT_SEC", "4"))
STOCKTWITS_TRENDING_TIMEOUT_SEC = int(os.getenv("STOCKTWITS_TRENDING_TIMEOUT_SEC", "4"))
APEWISDOM_ENABLED = os.getenv("APEWISDOM_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
APEWISDOM_TIMEOUT_SEC = int(os.getenv("APEWISDOM_TIMEOUT_SEC", "4"))
APEWISDOM_MAX_RESULTS = int(os.getenv("APEWISDOM_MAX_RESULTS", "80"))
SOCIAL_SKIP_REDDIT_IF_FALLBACK_READY = os.getenv("SOCIAL_SKIP_REDDIT_IF_FALLBACK_READY", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
}
# Third-party news source, OFF by default: the app ships without calling any
# non-public gateway API. Opt in explicitly by setting INTELLECTIA_API_BASE.
INTELLECTIA_API_BASE = os.getenv("INTELLECTIA_API_BASE", "").rstrip("/")
SYMBOL_MASTER_URLS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]
ETF_POPULAR_LIMIT = int(os.getenv("ETF_POPULAR_LIMIT", "250"))
ETF_SNAPSHOT_TTL_HOURS = int(os.getenv("ETF_SNAPSHOT_TTL_HOURS", "24"))
ETF_RESEARCH_SCAN_URL = "https://scanner.tradingview.com/america/scan"
AFTER_HOURS_SNAPSHOT_MAX_AGE_HOURS = int(os.getenv("AFTER_HOURS_SNAPSHOT_MAX_AGE_HOURS", "24"))
TICKER_EXCLUDE = {
    "A",
    "AI",
    "ALL",
    "ARE",
    "AT",
    "BE",
    "CEO",
    "DD",
    "EPS",
    "EV",
    "FOR",
    "GDP",
    "FOMC",
    "HOLD",
    "IT",
    "IRA",
    "IMO",
    "IRS",
    "LOL",
    "LONG",
    "MOON",
    "NEWS",
    "NOW",
    "OPEN",
    "PUTS",
    "CALLS",
    "SEC",
    "SELL",
    "SHORT",
    "THE",
    "THIS",
    "US",
    "USA",
    "USD",
    "YOLO",
}
SENTIMENT_BULL = ["call", "calls", "moon", "buy", "long", "bull", "bullish", "breakout", "squeeze", "green"]
SENTIMENT_BEAR = ["put", "puts", "short", "dump", "sell", "bear", "bearish", "rug", "down", "red"]
FINANCE_CONTEXT_TERMS = [
    "call",
    "calls",
    "put",
    "puts",
    "shares",
    "earnings",
    "guidance",
    "pt",
    "price target",
    "position",
    "squeeze",
    "yolo",
    "long",
    "short",
    "buy",
    "sell",
]
AMBIGUOUS_TICKERS = {
    "A",
    "AI",
    "ALL",
    "C",
    "DD",
    "F",
    "IT",
    "ON",
    "SO",
    "T",
    "US",
}
SCORING_MODEL_ID = "social_v4_tradability_context"
SOURCE_FAMILY_WEIGHTS = {
    "reddit": 1.0,
    "stocktwits": 1.15,
    "tiktok": 1.1,
    "apewisdom": 1.05,
}
CATALYST_KEYWORDS = {
    "earnings": ["earnings", "eps", "guidance"],
    "analyst": ["upgrade", "downgrade", "price target", "pt"],
    "corporate": ["merger", "acquisition", "buyback", "spin-off", "spinoff"],
    "regulatory": ["fda", "sec", "approval", "investigation", "antitrust"],
    "macro": ["fomc", "cpi", "ppi", "jobs report", "nfp"],
}
OPTIONS_CONTEXT_TERMS = ["call", "calls", "put", "puts", "strike", "expiry", "exp", "iv", "gamma", "delta", "0dte"]
MARKET_CAP_MULTIPLIER = {
    "Mega": 1.0,
    "Large": 0.96,
    "Mid": 0.88,
    "Small": 0.76,
    "Micro": 0.62,
}
CORS_ALLOW_ORIGINS = [
    x.strip()
    for x in os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3500,http://127.0.0.1:3500",
    ).split(",")
    if x.strip()
]
SOCIAL_API_KEY = os.getenv("SOCIAL_API_KEY", "").strip()
API_RATE_LIMIT_WINDOW_SEC = int(os.getenv("API_RATE_LIMIT_WINDOW_SEC", "60"))
API_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("API_RATE_LIMIT_MAX_REQUESTS", "300"))
SOCIAL_RATE_LIMIT_WINDOW_SEC = int(os.getenv("SOCIAL_RATE_LIMIT_WINDOW_SEC", "60"))
SOCIAL_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("SOCIAL_RATE_LIMIT_MAX_REQUESTS", "120"))
SOCIAL_SCAN_TIME_BUDGET_SEC = float(os.getenv("SOCIAL_SCAN_TIME_BUDGET_SEC", "30"))
SOCIAL_MAX_STOCKTWITS_SYMBOLS = int(os.getenv("SOCIAL_MAX_STOCKTWITS_SYMBOLS", "8"))
SOCIAL_QUOTE_VALIDATION_ENABLED = os.getenv("SOCIAL_QUOTE_VALIDATION_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
}
QUOTE_PROVIDER_AUTH_COOLDOWN_SEC = int(os.getenv("QUOTE_PROVIDER_AUTH_COOLDOWN_SEC", "900"))
SCREENER_HISTORY_WORKERS = int(os.getenv("SCREENER_HISTORY_WORKERS", "6"))
# Daily OHLCV barely changes intraday: cache aggressively to conserve provider quota.
HISTORY_CACHE_TTL_SEC = int(os.getenv("HISTORY_CACHE_TTL_SEC", "600"))
QUOTE_CACHE_TTL_SEC = int(os.getenv("QUOTE_CACHE_TTL_SEC", "60"))


SOCIAL_BASELINE_DB = os.path.join(os.path.dirname(__file__), "social_baseline.db")
SYMBOL_ALIAS_SEED_PATH = os.getenv(
    "SYMBOL_ALIAS_SEED_PATH",
    os.path.join(os.path.dirname(__file__), "symbol_alias_seed.json"),
)
SOCIAL_SOURCE_FAIL_THRESHOLD = int(os.getenv("SOCIAL_SOURCE_FAIL_THRESHOLD", "3"))
SOCIAL_SOURCE_COOLDOWN_SEC = int(os.getenv("SOCIAL_SOURCE_COOLDOWN_SEC", "120"))
