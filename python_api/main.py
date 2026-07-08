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

app = FastAPI(title=APP_NAME, version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS or ["http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_social_db_lock = threading.Lock()
_social_source_state_lock = threading.Lock()
_reddit_rate_lock = threading.Lock()
_rate_limit_lock = threading.Lock()
_reddit_last_request_ts = 0.0
_rate_limit_state: dict[str, tuple[float, int]] = {}
_social_source_failures: dict[str, int] = {}
_social_source_cooldown_until: dict[str, float] = {}
_quote_provider_auth_cooldown_until = 0.0
_provider_state_lock = threading.Lock()
_provider_cooldown_until: dict[str, float] = {}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Only trust X-Forwarded-For when explicitly running behind a proxy that
# OVERWRITES it. Otherwise any client can spoof the header to rotate its
# apparent IP and defeat the per-IP rate limiter.
TRUST_FORWARDED_FOR = os.getenv("TRUST_FORWARDED_FOR", "0").strip().lower() not in ("", "0", "false", "no")


def client_ip(request: FastAPIRequest) -> str:
    if TRUST_FORWARDED_FOR:
        forwarded = str(request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return str(request.client.host)
    return "unknown"


def enforce_social_api_key(request: FastAPIRequest) -> None:
    if not SOCIAL_API_KEY:
        return
    header_key = str(request.headers.get("x-api-key") or "").strip()
    auth = str(request.headers.get("authorization") or "").strip()
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    supplied = header_key or bearer
    if supplied != SOCIAL_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


def rate_limit_check(scope_key: str, subject_key: str, max_requests: int, window_sec: int) -> tuple[bool, int]:
    now = time.time()
    key = f"{scope_key}:{subject_key}"
    with _rate_limit_lock:
        started, count = _rate_limit_state.get(key, (now, 0))
        if now - started >= max(1, window_sec):
            started, count = now, 0
        count += 1
        _rate_limit_state[key] = (started, count)
        remaining = max(0, int(max_requests) - count)
        if count > int(max_requests):
            retry_after = max(1, int(math.ceil(max(0.0, window_sec - (now - started)))))
            return False, retry_after
        return True, remaining


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


# ---------------------------------------------------------------------------
# Generic stale-while-revalidate serving.
#
# Slow read endpoints (discovery scans, RSS pulls, per-symbol history sweeps)
# should never block a request: swr_serve() answers instantly from the last
# completed result and refreshes in a single-flight background thread when the
# result is older than refresh_after_sec. Results persist to SQLite so dev
# reloads / restarts don't cause a cold start. The only time a request waits
# is the very first computation ever for a key (nothing to serve yet).
# ---------------------------------------------------------------------------
_swr_lock = threading.Lock()
_swr_states: dict[str, dict[str, Any]] = {}


def _swr_persist(key: str, payload: dict[str, Any], completed_at: float) -> None:
    try:
        blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    except Exception as exc:
        logger.warning("SWR persist skipped for %s (not JSON-serializable): %s", key, exc)
        return
    try:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO swr_cache (key, payload_json, completed_at) VALUES (?, ?, ?)",
                    (key, blob, completed_at),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        logger.warning("SWR persist failed for %s: %s", key, exc)


def _swr_load_persisted(key: str) -> tuple[dict[str, Any], float] | None:
    try:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                row = conn.execute(
                    "SELECT payload_json, completed_at FROM swr_cache WHERE key = ?", (key,)
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        return json.loads(row[0]), float(row[1])
    except Exception:
        return None


def swr_serve(
    key: str,
    producer: Any,
    refresh_after_sec: int,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (payload, cache_meta), refreshing via `producer()` in the background."""
    now = time.time()
    with _swr_lock:
        state = _swr_states.setdefault(
            key,
            {"payload": None, "completed_at": 0.0, "running": False, "last_error": None, "loaded": False},
        )
        needs_load = not state["loaded"]
        state["loaded"] = True

    if needs_load:
        persisted = _swr_load_persisted(key)
        if persisted:
            with _swr_lock:
                state = _swr_states[key]
                if state["payload"] is None:
                    state["payload"], state["completed_at"] = persisted

    with _swr_lock:
        state = _swr_states[key]
        payload = state["payload"]
        completed_at = float(state["completed_at"] or 0.0)
        running = bool(state["running"])
        last_error = state["last_error"]

    if payload is None:
        # First-ever computation for this key: run inline so the caller gets data.
        result = producer()
        done = time.time()
        with _swr_lock:
            state = _swr_states[key]
            state["payload"] = result
            state["completed_at"] = done
            state["last_error"] = None
        _swr_persist(key, result, done)
        return result, {"age_sec": 0, "refreshing": False, "stale": False, "refresh_after_sec": refresh_after_sec, "last_error": None}

    age_sec = int(now - completed_at) if completed_at else None
    stale = age_sec is None or age_sec > max(15, refresh_after_sec)

    if (stale or force_refresh) and not running:
        def _worker() -> None:
            try:
                result = producer()
                done = time.time()
                with _swr_lock:
                    st = _swr_states[key]
                    st["payload"] = result
                    st["completed_at"] = done
                    st["last_error"] = None
                _swr_persist(key, result, done)
            except Exception as exc:
                logger.warning("SWR background refresh failed for %s: %s", key, exc)
                with _swr_lock:
                    _swr_states[key]["last_error"] = str(exc)
            finally:
                with _swr_lock:
                    _swr_states[key]["running"] = False

        with _swr_lock:
            state = _swr_states[key]
            if not state["running"]:
                state["running"] = True
                threading.Thread(target=_worker, daemon=True, name=f"swr:{key}").start()
        running = True

    meta = {
        "age_sec": age_sec,
        "refreshing": running,
        "stale": stale,
        "refresh_after_sec": refresh_after_sec,
        "last_error": last_error,
    }
    return payload, meta


_social_db_schema_ready = False


def _social_db_conn() -> sqlite3.Connection:
    global _social_db_schema_ready
    conn = sqlite3.connect(SOCIAL_BASELINE_DB)
    conn.execute("PRAGMA busy_timeout = 5000")
    if _social_db_schema_ready:
        # Schema DDL already ran this process — skip ~15 statements per open.
        return conn
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS social_baseline (
            ticker TEXT NOT NULL,
            bucket_hour INTEGER NOT NULL,
            mentions INTEGER NOT NULL,
            unique_authors INTEGER NOT NULL,
            engagement REAL NOT NULL,
            PRIMARY KEY (ticker, bucket_hour)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_social_baseline_ticker_hour ON social_baseline (ticker, bucket_hour)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_master_snapshot (
            snapshot_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            exchange TEXT NOT NULL,
            is_etf INTEGER NOT NULL,
            asset_type TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, symbol)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbol_master_snapshot_date ON symbol_master_snapshot (snapshot_date)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_master_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_alias (
            alias_symbol TEXT NOT NULL,
            canonical_symbol TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (alias_symbol, effective_from)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_alias_alias ON symbol_alias (alias_symbol)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_validity_cache (
            symbol TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            is_valid INTEGER NOT NULL,
            resolved_symbol TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            snapshot_source TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (symbol, as_of_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quote_liveness_cache (
            symbol TEXT NOT NULL,
            provider TEXT NOT NULL,
            bucket_minute INTEGER NOT NULL,
            is_live INTEGER NOT NULL,
            last_price REAL,
            checked_at TEXT NOT NULL,
            error TEXT,
            PRIMARY KEY (symbol, provider, bucket_minute)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quote_liveness_lookup ON quote_liveness_cache (symbol, provider, bucket_minute)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS popular_etf_snapshot (
            snapshot_date TEXT NOT NULL,
            popularity_rank INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            exchange TEXT NOT NULL,
            price REAL NOT NULL,
            change_pct REAL,
            volume REAL NOT NULL,
            avg_volume REAL,
            industry TEXT,
            category TEXT,
            issuer TEXT,
            expense_ratio REAL,
            total_assets REAL,
            largest_holdings_json TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, symbol)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_popular_etf_snapshot_date_rank ON popular_etf_snapshot (snapshot_date, popularity_rank)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS after_hours_snapshot (
            snapshot_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            asset_group TEXT NOT NULL,
            price REAL NOT NULL,
            percent_change REAL,
            volume REAL NOT NULL,
            market_cap REAL,
            market_cap_bucket TEXT,
            session_type TEXT,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_at, symbol)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_after_hours_snapshot_lookup ON after_hours_snapshot (snapshot_at, percent_change)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS social_signal_run (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            model_id TEXT NOT NULL,
            experiment_id TEXT,
            market_regime TEXT,
            row_count INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS social_signal_row (
            run_id TEXT NOT NULL,
            rank_position INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            rank_score REAL NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, rank_position)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_social_signal_row_ticker_created ON social_signal_row (ticker, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS swr_cache (
            key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            completed_at REAL NOT NULL
        )
        """
    )
    _social_db_schema_ready = True
    return conn


def upsert_social_baseline(bucket_hour: int, rows: dict[str, dict[str, float]]) -> None:
    if not rows:
        return
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            for ticker, metrics in rows.items():
                mentions = int(metrics.get("mentions") or 0)
                unique_authors = int(metrics.get("unique_authors") or 0)
                engagement = float(metrics.get("engagement") or 0.0)
                conn.execute(
                    """
                    INSERT INTO social_baseline (ticker, bucket_hour, mentions, unique_authors, engagement)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(ticker, bucket_hour) DO UPDATE SET
                        mentions = MAX(mentions, excluded.mentions),
                        unique_authors = MAX(unique_authors, excluded.unique_authors),
                        engagement = MAX(engagement, excluded.engagement)
                    """,
                    (ticker, bucket_hour, mentions, unique_authors, engagement),
                )
            # keep ~45d
            cutoff = bucket_hour - (45 * 24)
            conn.execute("DELETE FROM social_baseline WHERE bucket_hour < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()


def prune_sqlite_caches() -> dict[str, int]:
    """Retention pass over cache tables that are not pruned at write time.

    social_baseline (45d), social_signal_run/row (14d), and symbol_master_snapshot
    (540d) are already pruned where they are written.
    """
    now = datetime.now(UTC)
    validity_cutoff = (now - timedelta(days=180)).date().isoformat()
    liveness_cutoff_minute = int((now - timedelta(days=3)).timestamp() // 60)
    after_hours_cutoff = (now - timedelta(days=30)).isoformat()
    etf_cutoff = (now - timedelta(days=60)).date().isoformat()

    deleted: dict[str, int] = {}
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            cur = conn.execute("DELETE FROM symbol_validity_cache WHERE as_of_date < ?", (validity_cutoff,))
            deleted["symbol_validity_cache"] = cur.rowcount
            cur = conn.execute("DELETE FROM quote_liveness_cache WHERE bucket_minute < ?", (liveness_cutoff_minute,))
            deleted["quote_liveness_cache"] = cur.rowcount
            cur = conn.execute("DELETE FROM after_hours_snapshot WHERE snapshot_at < ?", (after_hours_cutoff,))
            deleted["after_hours_snapshot"] = cur.rowcount
            cur = conn.execute("DELETE FROM popular_etf_snapshot WHERE snapshot_date < ?", (etf_cutoff,))
            deleted["popular_etf_snapshot"] = cur.rowcount
            conn.commit()
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()
    total = sum(max(0, n) for n in deleted.values())
    if total:
        logger.info("SQLite retention pass removed %d rows: %s", total, deleted)
    return deleted


def _schedule_daily_prune() -> None:
    def run() -> None:
        try:
            prune_sqlite_caches()
        except Exception as exc:
            logger.warning("SQLite retention pass failed: %s", exc)
        finally:
            _schedule_daily_prune()

    timer = threading.Timer(24 * 3600, run)
    timer.daemon = True
    timer.start()


@app.on_event("startup")
def _startup_retention() -> None:
    try:
        prune_sqlite_caches()
    except Exception as exc:
        logger.warning("SQLite retention pass failed at startup: %s", exc)
    _schedule_daily_prune()


def social_baseline_stats(tickers: list[str], now_ts: float) -> dict[str, dict[str, float]]:
    if not tickers:
        return {}
    end_bucket = int(now_ts // 3600) - 1
    start_bucket = end_bucket - (30 * 24) + 1
    out: dict[str, dict[str, float]] = {}

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            for ticker in tickers:
                rows = conn.execute(
                    """
                    SELECT mentions, unique_authors, engagement
                    FROM social_baseline
                    WHERE ticker = ? AND bucket_hour >= ? AND bucket_hour <= ?
                    """,
                    (ticker, start_bucket, end_bucket),
                ).fetchall()
                if not rows:
                    out[ticker] = {
                        "samples": 0,
                        "avg_mentions_1h_30d": 0.0,
                        "avg_unique_authors_1h_30d": 0.0,
                        "avg_engagement_1h_30d": 0.0,
                        "std_engagement_1h_30d": 0.0,
                    }
                    continue
                mentions_arr = [float(r[0]) for r in rows]
                authors_arr = [float(r[1]) for r in rows]
                engagement_arr = [float(r[2]) for r in rows]
                avg_mentions = sum(mentions_arr) / len(mentions_arr)
                avg_authors = sum(authors_arr) / len(authors_arr)
                avg_eng = sum(engagement_arr) / len(engagement_arr)
                variance = sum((x - avg_eng) ** 2 for x in engagement_arr) / max(1, len(engagement_arr))
                std_eng = math.sqrt(variance)
                out[ticker] = {
                    "samples": float(len(rows)),
                    "avg_mentions_1h_30d": avg_mentions,
                    "avg_unique_authors_1h_30d": avg_authors,
                    "avg_engagement_1h_30d": avg_eng,
                    "std_engagement_1h_30d": std_eng,
                }
        finally:
            conn.close()
    return out


def persist_social_signal_run(
    rows: list[dict[str, Any]],
    *,
    model_id: str,
    market_regime: str,
    experiment_id: str | None = None,
) -> str:
    if not rows:
        return ""
    run_id = f"run_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    created_at = now_iso()
    sample_rows = rows[:50]

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            conn.execute(
                """
                INSERT INTO social_signal_run (run_id, created_at, model_id, experiment_id, market_regime, row_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    created_at,
                    model_id,
                    (experiment_id or None),
                    market_regime,
                    len(sample_rows),
                ),
            )
            for idx, row in enumerate(sample_rows, start=1):
                conn.execute(
                    """
                    INSERT INTO social_signal_row (run_id, rank_position, ticker, rank_score, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        idx,
                        str(row.get("ticker") or ""),
                        float(row.get("rank_score") or 0.0),
                        json.dumps(row, separators=(",", ":"), ensure_ascii=True),
                        created_at,
                    ),
                )
            # Keep the most recent 14 days of signal-lab rows.
            cutoff = (datetime.now(UTC) - timedelta(days=14)).isoformat()
            stale_runs = conn.execute(
                "SELECT run_id FROM social_signal_run WHERE created_at < ?",
                (cutoff,),
            ).fetchall()
            for rr in stale_runs:
                old_run_id = str(rr[0] or "")
                if not old_run_id:
                    continue
                conn.execute("DELETE FROM social_signal_row WHERE run_id = ?", (old_run_id,))
                conn.execute("DELETE FROM social_signal_run WHERE run_id = ?", (old_run_id,))
            conn.commit()
        finally:
            conn.close()
    return run_id


def append_mentions_from_rows(mentions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    for row in rows:
        text = f"{row.get('title', '')}\n{row.get('text', '')}"
        sentiment = classify_sentiment(text)
        tickers = row.get("tickers") or []
        for ticker in tickers:
            mentions.append(
                {
                    "ticker": ticker,
                    "source": row.get("source"),
                    "origin": row.get("origin"),
                    "sentiment": sentiment,
                    "author": row.get("author"),
                    "engagement": int(row.get("engagement") or 0),
                    "created_utc": float(row.get("created_utc") or 0),
                    "text": text,
                    "url": row.get("url") or "",
                }
            )


def fetch_json_url(
    url: str,
    timeout: int = 8,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str | None = None,
) -> Any:
    req_headers = {
        "User-Agent": random.choice(SOCIAL_USER_AGENTS),
        "Accept": "application/json,text/plain,*/*",
    }
    if headers:
        req_headers.update(headers)
    req = UrlRequest(
        url,
        headers=req_headers,
        data=data,
        method=method,
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw)


def fetch_text_url(url: str, timeout: int = 8) -> str:
    req = UrlRequest(url, headers={"User-Agent": random.choice(SOCIAL_USER_AGENTS)})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def reddit_rate_wait() -> None:
    global _reddit_last_request_ts
    with _reddit_rate_lock:
        now = time.time()
        elapsed = now - _reddit_last_request_ts
        if elapsed < REDDIT_MIN_INTERVAL_SEC:
            time.sleep(REDDIT_MIN_INTERVAL_SEC - elapsed)
        _reddit_last_request_ts = time.time()


def source_cooldown_remaining_sec(source_key: str) -> int:
    now = time.time()
    with _social_source_state_lock:
        until = float(_social_source_cooldown_until.get(source_key, 0.0))
    if until <= now:
        return 0
    return int(max(0, round(until - now)))


def source_record_success(source_key: str) -> None:
    with _social_source_state_lock:
        _social_source_failures[source_key] = 0
        _social_source_cooldown_until[source_key] = 0.0


def source_record_failure(source_key: str) -> tuple[int, int]:
    now = time.time()
    with _social_source_state_lock:
        failures = int(_social_source_failures.get(source_key, 0)) + 1
        _social_source_failures[source_key] = failures
        if failures >= max(1, SOCIAL_SOURCE_FAIL_THRESHOLD):
            cooldown_until = now + max(1, SOCIAL_SOURCE_COOLDOWN_SEC)
            _social_source_cooldown_until[source_key] = cooldown_until
            return failures, int(max(0, round(cooldown_until - now)))
    return failures, 0


def quote_provider_cooldown_remaining_sec() -> int:
    now = time.time()
    with _social_source_state_lock:
        until = float(_quote_provider_auth_cooldown_until)
    if until <= now:
        return 0
    return int(max(0, round(until - now)))


def quote_provider_record_auth_failure() -> None:
    now = time.time()
    with _social_source_state_lock:
        global _quote_provider_auth_cooldown_until
        _quote_provider_auth_cooldown_until = max(
            float(_quote_provider_auth_cooldown_until),
            now + max(30, QUOTE_PROVIDER_AUTH_COOLDOWN_SEC),
        )


def quote_provider_record_success() -> None:
    with _social_source_state_lock:
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


def refresh_symbol_alias_seed(force: bool = False) -> dict[str, Any]:
    today = datetime.now(UTC).date().isoformat()
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            if not force:
                refreshed_row = conn.execute(
                    "SELECT value FROM symbol_master_meta WHERE key = ?",
                    ("alias_seed_refreshed_on",),
                ).fetchone()
                if refreshed_row and str(refreshed_row[0] or "") == today:
                    count_row = conn.execute("SELECT COUNT(*) FROM symbol_alias").fetchone()
                    return {
                        "refreshed": False,
                        "reason": "already_refreshed_today",
                        "seed_path": SYMBOL_ALIAS_SEED_PATH,
                        "alias_count": int(count_row[0] or 0) if count_row else 0,
                        "refreshed_on": today,
                    }

            try:
                with open(SYMBOL_ALIAS_SEED_PATH, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except FileNotFoundError:
                raw = []
            except Exception as exc:
                logger.warning("Symbol alias seed unreadable (%s): %s", SYMBOL_ALIAS_SEED_PATH, exc)
                raw = []

            entries = raw if isinstance(raw, list) else []
            normalized: list[tuple[str, str, str, str | None, str]] = []
            for row in entries:
                if not isinstance(row, dict):
                    continue
                alias_symbol = str(row.get("alias_symbol") or "").upper().strip()
                canonical_symbol = str(row.get("canonical_symbol") or "").upper().strip()
                effective_from = normalize_as_of_date(str(row.get("effective_from") or "2000-01-01"))
                effective_to_raw = str(row.get("effective_to") or "").strip()
                effective_to = normalize_as_of_date(effective_to_raw) if effective_to_raw else None
                source = str(row.get("source") or "seed").strip() or "seed"
                if not alias_symbol or not canonical_symbol:
                    continue
                if alias_symbol == canonical_symbol:
                    continue
                normalized.append((alias_symbol, canonical_symbol, effective_from, effective_to, source))

            conn.execute("DELETE FROM symbol_alias WHERE source IN ('seed', 'seed_manual')")
            for alias_symbol, canonical_symbol, effective_from, effective_to, source in normalized:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO symbol_alias
                    (alias_symbol, canonical_symbol, effective_from, effective_to, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alias_symbol,
                        canonical_symbol,
                        effective_from,
                        effective_to,
                        source,
                        now_iso(),
                    ),
                )

            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_master_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                ("alias_seed_refreshed_on", today, now_iso()),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_master_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                ("alias_seed_refreshed_count", str(len(normalized)), now_iso()),
            )
            conn.commit()
            return {
                "refreshed": True,
                "seed_path": SYMBOL_ALIAS_SEED_PATH,
                "alias_count": len(normalized),
                "refreshed_on": today,
            }
        finally:
            conn.close()


def symbol_alias_health() -> dict[str, Any]:
    refresh_symbol_alias_seed(force=False)
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            total_row = conn.execute("SELECT COUNT(*) FROM symbol_alias").fetchone()
            sample_rows = conn.execute(
                """
                SELECT alias_symbol, canonical_symbol, effective_from, effective_to, source
                FROM symbol_alias
                ORDER BY alias_symbol ASC
                LIMIT 30
                """
            ).fetchall()
            refreshed_on = conn.execute(
                "SELECT value FROM symbol_master_meta WHERE key = ?",
                ("alias_seed_refreshed_on",),
            ).fetchone()
            refreshed_count = conn.execute(
                "SELECT value FROM symbol_master_meta WHERE key = ?",
                ("alias_seed_refreshed_count",),
            ).fetchone()
        finally:
            conn.close()

    return {
        "updated_at": now_iso(),
        "seed_path": SYMBOL_ALIAS_SEED_PATH,
        "alias_count": int(total_row[0] or 0) if total_row else 0,
        "last_refreshed_on": str(refreshed_on[0] or "") if refreshed_on else "",
        "last_refreshed_count": int(refreshed_count[0] or 0) if refreshed_count else 0,
        "sample": [
            {
                "alias_symbol": str(r[0] or ""),
                "canonical_symbol": str(r[1] or ""),
                "effective_from": str(r[2] or ""),
                "effective_to": str(r[3] or "") if r[3] is not None else None,
                "source": str(r[4] or ""),
            }
            for r in sample_rows
        ],
    }


def load_symbol_master() -> tuple[dict[str, dict[str, Any]], str, str]:
    refresh_symbol_alias_seed(force=False)
    cache_key = "social:symbol_master:v2"
    cached = get_cached(cache_key, 24 * 3600)
    if cached is not None:
        return cached

    def fetch_remote_symbol_master() -> dict[str, dict[str, Any]]:
        symbols: dict[str, dict[str, Any]] = {}

        # NASDAQ listed
        try:
            txt = fetch_text_url(SYMBOL_MASTER_URLS[0], timeout=10)
            lines = [ln for ln in txt.splitlines() if ln and not ln.startswith("File Creation Time")]
            if lines:
                headers = lines[0].split("|")
                for ln in lines[1:]:
                    parts = ln.split("|")
                    if len(parts) != len(headers):
                        continue
                    row = dict(zip(headers, parts))
                    sym = str(row.get("Symbol") or "").strip().upper()
                    if not sym:
                        continue
                    name = str(row.get("Security Name") or "")
                    if row.get("Test Issue") == "Y":
                        continue
                    if any(x in name.lower() for x in ["warrant", "rights", "unit", "preferred"]):
                        continue
                    symbols[sym] = {
                        "symbol": sym,
                        "name": name,
                        "exchange": "NASDAQ",
                        "is_etf": row.get("ETF") == "Y",
                        "asset_type": "etf" if row.get("ETF") == "Y" else "equity",
                    }
        except Exception as exc:
            logger.warning("Symbol master fetch failed (%s): %s", SYMBOL_MASTER_URLS[0], exc)

        # NYSE/AMEX/other listed
        try:
            txt = fetch_text_url(SYMBOL_MASTER_URLS[1], timeout=10)
            lines = [ln for ln in txt.splitlines() if ln and not ln.startswith("File Creation Time")]
            if lines:
                headers = lines[0].split("|")
                for ln in lines[1:]:
                    parts = ln.split("|")
                    if len(parts) != len(headers):
                        continue
                    row = dict(zip(headers, parts))
                    sym = str(row.get("ACT Symbol") or row.get("NASDAQ Symbol") or "").strip().upper()
                    if not sym:
                        continue
                    name = str(row.get("Security Name") or "")
                    if row.get("Test Issue") == "Y":
                        continue
                    if any(x in name.lower() for x in ["warrant", "rights", "unit", "preferred"]):
                        continue
                    ex = str(row.get("Exchange") or "")
                    symbols[sym] = {
                        "symbol": sym,
                        "name": name,
                        "exchange": ex or "OTHER",
                        "is_etf": row.get("ETF") == "Y",
                        "asset_type": "etf" if row.get("ETF") == "Y" else "equity",
                    }
        except Exception as exc:
            logger.warning("Symbol master fetch failed (%s): %s", SYMBOL_MASTER_URLS[1], exc)
        return symbols

    def load_snapshot(conn: sqlite3.Connection, snapshot_date: str) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT symbol, name, exchange, is_etf, asset_type
            FROM symbol_master_snapshot
            WHERE snapshot_date = ?
            """,
            (snapshot_date,),
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for sym, name, ex, is_etf, asset_type in rows:
            symbol = str(sym or "").upper()
            if not symbol:
                continue
            out[symbol] = {
                "symbol": symbol,
                "name": str(name or ""),
                "exchange": str(ex or ""),
                "is_etf": bool(int(is_etf or 0)),
                "asset_type": str(asset_type or "equity"),
            }
        return out

    def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO symbol_master_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, now_iso()),
        )

    def persist_snapshot(conn: sqlite3.Connection, snapshot_date: str, symbols: dict[str, dict[str, Any]]) -> None:
        if not symbols:
            return
        for sym, item in symbols.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_master_snapshot
                (snapshot_date, symbol, name, exchange, is_etf, asset_type, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_date,
                    sym,
                    str(item.get("name") or ""),
                    str(item.get("exchange") or ""),
                    1 if bool(item.get("is_etf")) else 0,
                    str(item.get("asset_type") or "equity"),
                    "nasdaq_trader",
                ),
            )
        set_meta(conn, "latest_snapshot_date", snapshot_date)
        set_meta(conn, "latest_snapshot_source", "remote_refresh")
        set_meta(conn, "latest_snapshot_rows", str(len(symbols)))
        # Keep roughly 18 months of daily snapshots.
        cutoff = (datetime.now(UTC) - timedelta(days=540)).date().isoformat()
        conn.execute("DELETE FROM symbol_master_snapshot WHERE snapshot_date < ?", (cutoff,))

    snapshot_date = datetime.now(UTC).date().isoformat()
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            today_symbols = load_snapshot(conn, snapshot_date)
            if len(today_symbols) >= 2000:
                result = (today_symbols, snapshot_date, "sqlite_snapshot")
                set_cached(cache_key, result)
                return result

            remote_symbols = fetch_remote_symbol_master()
            if remote_symbols:
                persist_snapshot(conn, snapshot_date, remote_symbols)
                conn.commit()
                result = (remote_symbols, snapshot_date, "remote_refresh")
                set_cached(cache_key, result)
                return result

            latest = conn.execute(
                "SELECT value FROM symbol_master_meta WHERE key = ?",
                ("latest_snapshot_date",),
            ).fetchone()
            latest_date = str(latest[0]) if latest and latest[0] else ""
            if latest_date:
                latest_symbols = load_snapshot(conn, latest_date)
                if latest_symbols:
                    try:
                        age_days = (datetime.now(UTC).date() - datetime.fromisoformat(latest_date).date()).days
                    except Exception:
                        age_days = -1
                    if age_days > 7:
                        logger.warning(
                            "Symbol master remote refresh failed; serving stale snapshot %s (%d days old)",
                            latest_date,
                            age_days,
                        )
                    set_meta(conn, "latest_snapshot_source", "sqlite_fallback")
                    set_meta(conn, "latest_snapshot_rows", str(len(latest_symbols)))
                    conn.commit()
                    result = (latest_symbols, latest_date, "sqlite_fallback")
                    set_cached(cache_key, result)
                    return result

            # Last-resort fallback to today rows (possibly partial)
            logger.warning("Symbol master remote refresh failed and no prior snapshot; serving partial rows (%d)", len(today_symbols))
            set_meta(conn, "latest_snapshot_source", "sqlite_partial")
            set_meta(conn, "latest_snapshot_rows", str(len(today_symbols)))
            conn.commit()
            result = (today_symbols, snapshot_date, "sqlite_partial")
            set_cached(cache_key, result)
            return result
        finally:
            conn.close()


def symbol_master_health() -> dict[str, Any]:
    # Ensure cache/snapshot path is exercised before reading health.
    symbols, snapshot_date, source = load_symbol_master()
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            latest_meta_rows = conn.execute(
                "SELECT key, value, updated_at FROM symbol_master_meta WHERE key IN ('latest_snapshot_date', 'latest_snapshot_source', 'latest_snapshot_rows')"
            ).fetchall()
            latest_meta: dict[str, dict[str, str]] = {}
            for key, value, updated_at in latest_meta_rows:
                latest_meta[str(key)] = {"value": str(value or ""), "updated_at": str(updated_at or "")}

            snapshot_count_row = conn.execute("SELECT COUNT(DISTINCT snapshot_date) FROM symbol_master_snapshot").fetchone()
            total_rows_row = conn.execute("SELECT COUNT(*) FROM symbol_master_snapshot").fetchone()
            latest_rows_row = conn.execute(
                "SELECT COUNT(*) FROM symbol_master_snapshot WHERE snapshot_date = ?",
                (snapshot_date,),
            ).fetchone()
            oldest_snapshot_row = conn.execute("SELECT MIN(snapshot_date) FROM symbol_master_snapshot").fetchone()
        finally:
            conn.close()

    days_since_snapshot: int | None = None
    try:
        days_since_snapshot = (datetime.now(UTC).date() - datetime.fromisoformat(snapshot_date).date()).days
    except Exception:
        days_since_snapshot = None

    return {
        "updated_at": now_iso(),
        "current_snapshot_date": snapshot_date,
        "current_snapshot_source": source,
        "current_snapshot_symbol_count": len(symbols),
        "days_since_snapshot": days_since_snapshot,
        "snapshot_stale": bool(days_since_snapshot is not None and days_since_snapshot > 7),
        "snapshot_count": int(snapshot_count_row[0] or 0) if snapshot_count_row else 0,
        "snapshot_total_rows": int(total_rows_row[0] or 0) if total_rows_row else 0,
        "latest_snapshot_rows": int(latest_rows_row[0] or 0) if latest_rows_row else 0,
        "oldest_snapshot_date": str(oldest_snapshot_row[0] or "") if oldest_snapshot_row else "",
        "meta": latest_meta,
    }


def symbol_master_snapshots(limit: int = 60) -> dict[str, Any]:
    cap = max(1, min(365, int(limit)))
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            rows = conn.execute(
                """
                SELECT snapshot_date, COUNT(*) AS rows_count
                FROM symbol_master_snapshot
                GROUP BY snapshot_date
                ORDER BY snapshot_date DESC
                LIMIT ?
                """,
                (cap,),
            ).fetchall()
        finally:
            conn.close()
    items = [{"snapshot_date": str(r[0] or ""), "rows": int(r[1] or 0)} for r in rows]
    return {"updated_at": now_iso(), "count": len(items), "rows": items}


def normalize_as_of_date(as_of: str | None = None) -> str:
    if not as_of:
        return datetime.now(UTC).date().isoformat()
    try:
        return datetime.fromisoformat(as_of).date().isoformat()
    except Exception:
        return datetime.now(UTC).date().isoformat()


def load_symbol_master_at_date(as_of_date: str) -> tuple[dict[str, dict[str, Any]], str, str]:
    target_date = normalize_as_of_date(as_of_date)
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            row = conn.execute(
                """
                SELECT snapshot_date
                FROM symbol_master_snapshot
                WHERE snapshot_date <= ?
                ORDER BY snapshot_date DESC
                LIMIT 1
                """,
                (target_date,),
            ).fetchone()
            snapshot_date = str(row[0]) if row and row[0] else ""
            if snapshot_date:
                rows = conn.execute(
                    """
                    SELECT symbol, name, exchange, is_etf, asset_type
                    FROM symbol_master_snapshot
                    WHERE snapshot_date = ?
                    """,
                    (snapshot_date,),
                ).fetchall()
                out: dict[str, dict[str, Any]] = {}
                for sym, name, ex, is_etf, asset_type in rows:
                    symbol = str(sym or "").upper()
                    if not symbol:
                        continue
                    out[symbol] = {
                        "symbol": symbol,
                        "name": str(name or ""),
                        "exchange": str(ex or ""),
                        "is_etf": bool(int(is_etf or 0)),
                        "asset_type": str(asset_type or "equity"),
                    }
                if out:
                    return out, snapshot_date, "historical_snapshot"
        finally:
            conn.close()
    return load_symbol_master()


def resolve_symbol_alias(symbol: str, as_of_date: str) -> tuple[str, str | None]:
    candidate = str(symbol or "").upper().strip()
    if not candidate:
        return "", None
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            row = conn.execute(
                """
                SELECT canonical_symbol
                FROM symbol_alias
                WHERE alias_symbol = ?
                  AND effective_from <= ?
                  AND (effective_to IS NULL OR effective_to >= ?)
                ORDER BY effective_from DESC
                LIMIT 1
                """,
                (candidate, as_of_date, as_of_date),
            ).fetchone()
            if row and row[0]:
                canonical = str(row[0]).upper().strip()
                if canonical:
                    return canonical, candidate
        finally:
            conn.close()
    return candidate, None


def resolve_symbol_validity(symbol: str, as_of_date: str) -> dict[str, Any]:
    refresh_symbol_alias_seed(force=False)
    as_of = normalize_as_of_date(as_of_date)
    input_symbol = str(symbol or "").upper().strip()
    if not input_symbol:
        return {
            "input_symbol": "",
            "resolved_symbol": "",
            "is_valid": False,
            "as_of_date": as_of,
            "snapshot_date": "",
            "snapshot_source": "none",
            "alias_applied": False,
            "alias_from": None,
            "cache": "none",
            "checked_at": now_iso(),
        }

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            cached = conn.execute(
                """
                SELECT is_valid, resolved_symbol, snapshot_date, snapshot_source, checked_at
                FROM symbol_validity_cache
                WHERE symbol = ? AND as_of_date = ?
                """,
                (input_symbol, as_of),
            ).fetchone()
            if cached:
                return {
                    "input_symbol": input_symbol,
                    "resolved_symbol": str(cached[1] or input_symbol),
                    "is_valid": bool(int(cached[0] or 0)),
                    "as_of_date": as_of,
                    "snapshot_date": str(cached[2] or ""),
                    "snapshot_source": str(cached[3] or ""),
                    "alias_applied": str(cached[1] or input_symbol) != input_symbol,
                    "alias_from": input_symbol if str(cached[1] or input_symbol) != input_symbol else None,
                    "cache": "sqlite_validity",
                    "checked_at": str(cached[4] or ""),
                }
        finally:
            conn.close()

    symbol_master, snapshot_date, snapshot_source = load_symbol_master_at_date(as_of)
    resolved_symbol, alias_from = resolve_symbol_alias(input_symbol, as_of)
    is_valid = resolved_symbol in symbol_master
    checked_at = now_iso()

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_validity_cache
                (symbol, as_of_date, is_valid, resolved_symbol, snapshot_date, snapshot_source, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    input_symbol,
                    as_of,
                    1 if is_valid else 0,
                    resolved_symbol,
                    snapshot_date,
                    snapshot_source,
                    checked_at,
                ),
            )
            # Keep about 6 months of validity lookups.
            cutoff = (datetime.now(UTC) - timedelta(days=180)).date().isoformat()
            conn.execute("DELETE FROM symbol_validity_cache WHERE as_of_date < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()

    return {
        "input_symbol": input_symbol,
        "resolved_symbol": resolved_symbol,
        "is_valid": is_valid,
        "as_of_date": as_of,
        "snapshot_date": snapshot_date,
        "snapshot_source": snapshot_source,
        "alias_applied": alias_from is not None,
        "alias_from": alias_from,
        "cache": "computed",
        "checked_at": checked_at,
    }


def mention_context_score(text: str, ticker: str) -> tuple[float, int]:
    src = (text or "").lower()
    cashtag_mentions = len(re.findall(rf"\${re.escape(ticker)}\b", text or "", flags=re.IGNORECASE))
    hits = 0
    for term in FINANCE_CONTEXT_TERMS:
        if term in src:
            hits += 1
    bonus = 2 if cashtag_mentions > 0 else 0
    score = min(1.0, (hits + bonus) / 6.0)
    return score, cashtag_mentions


def extract_tickers(text: str) -> set[str]:
    out: set[str] = set()
    for token in TICKER_PATTERN.findall(text or ""):
        if not token.startswith("$") and token != token.upper():
            continue
        sym = token.lstrip("$").upper()
        if sym in TICKER_EXCLUDE:
            continue
        out.add(sym)
    return out


def classify_sentiment(text: str) -> str:
    src = (text or "").lower()
    bull = sum(src.count(word) for word in SENTIMENT_BULL)
    bear = sum(src.count(word) for word in SENTIMENT_BEAR)
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return "neutral"


def weighted_source_signal(source_mentions: dict[str, int]) -> float:
    if not source_mentions:
        return 0.0
    score = 0.0
    for source, count in source_mentions.items():
        weight = float(SOURCE_FAMILY_WEIGHTS.get(source, 0.9))
        score += math.log1p(max(0, int(count))) * weight
    return score


def compute_mention_quality(
    *,
    raw_mentions: int,
    qualified_mentions: int,
    unique_authors: int,
    cashtag_mentions: int,
    avg_context: float,
    cross_source_count: int,
    weighted_source_score: float,
    ambiguous: bool,
) -> tuple[float, float]:
    if raw_mentions <= 0:
        return 0.0, 0.0
    qualified_ratio = qualified_mentions / max(1, raw_mentions)
    author_diversity = unique_authors / max(1, raw_mentions)
    cashtag_density = cashtag_mentions / max(1, raw_mentions)
    ambiguity_penalty = 0.2 if ambiguous else 0.0
    concentration_penalty = 0.18 if author_diversity < 0.35 else 0.0
    raw_score = (
        qualified_ratio * 0.42
        + min(1.0, avg_context) * 0.24
        + min(1.0, author_diversity) * 0.14
        + min(1.0, cashtag_density * 2.5) * 0.08
        + min(1.0, weighted_source_score / 3.5) * 0.12
        + (0.03 if cross_source_count >= 2 else 0.0)
        - ambiguity_penalty
        - concentration_penalty
    )
    return max(0.0, min(1.0, raw_score)), author_diversity


def confidence_tier_from_score(score: float) -> str:
    if score >= 2.6:
        return "A"
    if score >= 1.8:
        return "B"
    return "C"


def extract_catalyst_hits(text: str) -> dict[str, int]:
    src = (text or "").lower()
    out: dict[str, int] = {}
    for label, terms in CATALYST_KEYWORDS.items():
        hits = 0
        for term in terms:
            hits += src.count(term)
        if hits > 0:
            out[label] = hits
    return out


def options_context_hits(text: str) -> int:
    src = (text or "").lower()
    return sum(src.count(term) for term in OPTIONS_CONTEXT_TERMS)


def parse_reddit_listing(subreddit: str, sort: str, limit: int = 60) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload: dict[str, Any] | None = None
    origin = "reddit_json_public"
    safe_limit = max(5, min(limit, REDDIT_PUBLIC_LIMIT))
    fetch_attempts = 0
    retries_used = 0
    started = time.time()

    last_exc: Exception | None = None
    for attempt in range(max(1, REDDIT_FETCH_RETRIES)):
        fetch_attempts += 1
        try:
            reddit_rate_wait()
            payload = fetch_json_url(
                f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={safe_limit}&raw_json=1",
                timeout=max(2, REDDIT_FETCH_TIMEOUT_SEC),
                headers={"User-Agent": SOCIAL_REDDIT_USER_AGENT, "Accept": "application/json"},
            )
            retries_used = attempt
            break
        except Exception as exc:
            last_exc = exc
            if attempt < max(1, REDDIT_FETCH_RETRIES) - 1:
                time.sleep(REDDIT_BACKOFF_BASE_SEC * (2 ** attempt))

    if payload is None:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Reddit listing fetch failed for r/{subreddit}/{sort}")

    rows: list[dict[str, Any]] = []
    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        title = str(d.get("title") or "")
        body = str(d.get("selftext") or "")
        rows.append(
            {
                "source": f"reddit:{subreddit}",
                "origin": origin,
                "subreddit": subreddit,
                "author": str(d.get("author") or "unknown"),
                "created_utc": float(d.get("created_utc") or 0),
                "engagement": int(d.get("score") or 0) + int(d.get("num_comments") or 0),
                "title": title,
                "text": body,
                "url": f"https://www.reddit.com{d.get('permalink')}" if d.get("permalink") else "",
                "tickers": list(extract_tickers(f"{title}\n{body}")),
            }
        )
    elapsed_ms = int((time.time() - started) * 1000)
    meta = {
        "origin": origin,
        "attempts": fetch_attempts,
        "retries_used": retries_used,
        "elapsed_ms": elapsed_ms,
        "limit": safe_limit,
    }
    return rows, meta


def parse_old_reddit(subreddit: str, limit: int = 40) -> list[dict[str, Any]]:
    html = fetch_text_url(f"https://old.reddit.com/r/{subreddit}/")
    rows: list[dict[str, Any]] = []
    for match in re.finditer(
        r'<a class="title [^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        title = re.sub(r"<[^>]+>", "", unescape(match.group(2))).strip()
        url = unescape(match.group(1)).strip()
        if not title:
            continue
        rows.append(
            {
                "source": f"reddit:{subreddit}",
                "origin": "old_reddit_html",
                "subreddit": subreddit,
                "author": "unknown",
                "created_utc": time.time(),
                "engagement": 0,
                "title": title,
                "text": "",
                "url": url,
                "tickers": list(extract_tickers(title)),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def fetch_apewisdom_wallstreetbets(limit: int = 80) -> list[dict[str, Any]]:
    if not APEWISDOM_ENABLED:
        return []
    safe_limit = max(5, min(int(limit), max(5, APEWISDOM_MAX_RESULTS)))
    payload = fetch_json_url(
        "https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/1",
        timeout=APEWISDOM_TIMEOUT_SEC,
    )
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    now_ts = time.time()
    for raw in items[:safe_limit]:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").upper().strip()
        if not ticker or not SYMBOL_PATTERN.match(ticker):
            continue
        mentions = max(1, int(raw.get("mentions") or 0))
        upvotes = max(0, int(raw.get("upvotes") or 0))
        # Weighted synthetic mention for aggregated ranking source.
        rows.append(
            {
                "source": "apewisdom:wallstreetbets",
                "origin": "apewisdom_api",
                "subreddit": "wallstreetbets",
                "author": "apewisdom",
                "created_utc": now_ts,
                "engagement": upvotes,
                "title": f"{ticker} ranked on ApeWisdom WallStreetBets list",
                "text": f"${ticker} wallstreetbets mentions {mentions} upvotes {upvotes}",
                "url": f"https://apewisdom.io/stocks/{ticker.lower()}",
                "tickers": [ticker],
                "sentiment": "neutral",
                "mention_weight": mentions,
            }
        )
    return rows


def fetch_stocktwits_trending(limit: int = 40) -> list[dict[str, Any]]:
    safe_limit = max(5, min(int(limit), 80))
    payload = fetch_json_url(
        "https://api.stocktwits.com/api/2/trending/symbols.json",
        timeout=STOCKTWITS_TRENDING_TIMEOUT_SEC,
    )
    items = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    now_ts = time.time()
    for raw in items[:safe_limit]:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("symbol") or "").upper().strip()
        if not ticker or not SYMBOL_PATTERN.match(ticker):
            continue
        watchlist = max(0, int(raw.get("watchlist_count") or 0))
        rows.append(
            {
                "source": "stocktwits:trending",
                "origin": "stocktwits_trending_api",
                "subreddit": None,
                "author": "stocktwits",
                "created_utc": now_ts,
                "engagement": watchlist,
                "title": f"{ticker} trending on Stocktwits",
                "text": f"${ticker} stocktwits trending watchlist {watchlist}",
                "url": str(raw.get("deeplink") or ""),
                "tickers": [ticker],
                "sentiment": "neutral",
                "mention_weight": 2,
            }
        )
    return rows


def fetch_stocktwits_messages(symbol: str) -> list[dict[str, Any]]:
    payload = fetch_json_url(
        f"https://api.stocktwits.com/api/2/streams/symbol/{quote_plus(symbol)}.json",
        timeout=STOCKTWITS_FETCH_TIMEOUT_SEC,
    )
    rows = []
    for msg in payload.get("messages", [])[:40]:
        body = str(msg.get("body") or "")
        sentiment = str((msg.get("entities", {}).get("sentiment") or {}).get("basic") or "").lower()
        if sentiment not in {"bullish", "bearish"}:
            sentiment = classify_sentiment(body)
        created_at = str(msg.get("created_at") or "")
        created_utc = 0.0
        if created_at:
            try:
                created_utc = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
            except Exception:
                created_utc = time.time()
        rows.append(
            {
                "source": "stocktwits",
                "origin": "stocktwits_api",
                "subreddit": None,
                "author": str((msg.get("user") or {}).get("username") or "unknown"),
                "created_utc": created_utc,
                "engagement": int((msg.get("likes") or {}).get("total") or 0),
                "title": body[:120],
                "text": body,
                "url": str(msg.get("id") or ""),
                "tickers": [symbol],
                "sentiment": sentiment,
            }
        )
    return rows


def fetch_tiktok_messages(symbols: list[str], limit: int = 120) -> list[dict[str, Any]]:
    if not TIKTOK_PROVIDER_URL or not TIKTOK_PROVIDER_API_KEY or not symbols:
        return []

    body = json.dumps(
        {
            "symbols": sorted(set(s.upper() for s in symbols if s)),
            "limit": max(1, min(500, int(limit))),
        }
    ).encode("utf-8")

    payload = fetch_json_url(
        TIKTOK_PROVIDER_URL,
        timeout=TIKTOK_PROVIDER_TIMEOUT_SEC,
        headers={
            "Authorization": f"Bearer {TIKTOK_PROVIDER_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=body,
        method="POST",
    )

    raw_rows: list[Any]
    if isinstance(payload, dict):
        val = payload.get("rows")
        raw_rows = val if isinstance(val, list) else []
    elif isinstance(payload, list):
        raw_rows = payload
    else:
        raw_rows = []

    out: list[dict[str, Any]] = []
    accepted = set(s.upper() for s in symbols if s)
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("caption") or row.get("title") or "")
        if not text:
            continue
        found = row.get("tickers")
        if isinstance(found, list):
            tickers = [str(t).upper().strip() for t in found if str(t).strip()]
        else:
            tickers = list(extract_tickers(text))
        tickers = [t for t in tickers if t in accepted]
        if not tickers:
            continue

        created_utc = 0.0
        created_raw = row.get("created_utc")
        if created_raw is not None:
            try:
                created_utc = float(created_raw)
            except Exception:
                created_utc = 0.0
        if created_utc <= 0:
            created_at = str(row.get("created_at") or "")
            if created_at:
                try:
                    created_utc = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
                except Exception:
                    created_utc = time.time()
            else:
                created_utc = time.time()

        likes = int(row.get("likes") or 0)
        comments = int(row.get("comments") or row.get("comment_count") or 0)
        shares = int(row.get("shares") or row.get("share_count") or 0)
        engagement = likes + comments + shares

        out.append(
            {
                "source": "tiktok",
                "origin": "tiktok_provider",
                "subreddit": None,
                "author": str(row.get("author") or row.get("username") or "unknown"),
                "created_utc": created_utc,
                "engagement": engagement,
                "title": text[:120],
                "text": text,
                "url": str(row.get("url") or ""),
                "tickers": tickers,
                "sentiment": classify_sentiment(text),
            }
        )
    return out


def is_quote_provider_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ("invalid crumb" in msg) or ("unauthorized" in msg) or is_provider_exhausted_or_unauthorized_error(exc)


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

def validate_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if not SYMBOL_PATTERN.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid symbol: {symbol}")
    return value


def normalize_pct(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    pct = float(value)
    if not math.isfinite(pct):
        return None
    if -2 <= pct <= 2:
        pct *= 100
    return pct


def to_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        out = float(value)
        if not math.isfinite(out):
            return None
        return out
    except Exception:
        return None


def call_openbb_silenced(fn: Any, *args: Any, **kwargs: Any) -> Any:
    # OpenBB/yfinance can emit noisy stderr lines (e.g., Invalid Crumb) on
    # upstream auth failures. Capture them to keep API logs actionable.
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        return fn(*args, **kwargs)


def finite_or(value: Any, fallback: float = 0.0) -> float:
    parsed = to_float(value)
    return parsed if parsed is not None else float(fallback)


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
    round-trip) — use it for callers that don't display/filter on market cap
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


@app.get("/api/market/overview")
def market_overview() -> dict[str, Any]:
    payload, meta = swr_serve("market:overview", _build_market_overview, refresh_after_sec=60)
    return {**payload, "cache": meta}


def infer_after_hours_asset_group(asset_type: str) -> str:
    text = str(asset_type or "").strip().lower()
    if text in {"fund", "etf"}:
        return "etf"
    return "stocks"


def fetch_tradingview_after_hours(session_type: str, limit: int = 300) -> list[dict[str, Any]]:
    session = "postmarket" if str(session_type or "").lower().startswith("post") else "premarket"
    sort_field = f"{session}_change_abs"
    close_field = f"{session}_close"
    change_field = f"{session}_change"
    payload = {
        "range": [0, max(50, min(1000, int(limit)))],
        "sort": {"sortBy": sort_field, "sortOrder": "desc"},
        "columns": [
            "name",
            "description",
            "type",
            "exchange",
            "close",
            "volume",
            "market_cap_basic",
            close_field,
            change_field,
            sort_field,
        ],
    }
    raw = fetch_json_url(
        "https://scanner.tradingview.com/america/scan",
        timeout=max(6, MARKET_DATA_TIMEOUT_SEC),
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    items = raw.get("data") if isinstance(raw, dict) else []
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        full_symbol = str(item.get("s") or "").strip()
        if not full_symbol or full_symbol.startswith("OTC:"):
            continue
        symbol = full_symbol.split(":")[-1].upper().strip()
        fields = item.get("d") if isinstance(item.get("d"), list) else []
        if len(fields) < 10:
            continue
        asset_type = str(fields[2] or "").strip().lower()
        if asset_type not in {"stock", "fund", "dr", "etf"}:
            continue
        session_pct = to_float(fields[8])
        if session_pct is None:
            continue
        market_cap = to_float(fields[6])
        out.append(
            {
                "symbol": symbol,
                "name": str(fields[1] or symbol),
                "asset_group": infer_after_hours_asset_group(asset_type),
                "price": finite_or(fields[7], 0),
                "percent_change": session_pct,
                "volume": finite_or(fields[5], 0),
                "market_cap": market_cap,
                "market_cap_bucket": market_cap_bucket(market_cap),
                "session_type": "post" if session == "postmarket" else "pre",
                "source_provider": "research_tradingview",
            }
        )
    return out


def persist_after_hours_snapshot(conn: sqlite3.Connection, snapshot_at: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    conn.execute("DELETE FROM after_hours_snapshot WHERE snapshot_at = ?", (snapshot_at,))
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO after_hours_snapshot
            (snapshot_at, symbol, name, asset_group, price, percent_change, volume, market_cap, market_cap_bucket, session_type, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_at,
                str(row.get("symbol") or ""),
                str(row.get("name") or ""),
                str(row.get("asset_group") or "stocks"),
                finite_or(row.get("price"), 0),
                to_float(row.get("percent_change")),
                finite_or(row.get("volume"), 0),
                to_float(row.get("market_cap")),
                str(row.get("market_cap_bucket") or ""),
                str(row.get("session_type") or ""),
                str(row.get("source_provider") or "research_tradingview"),
                now_iso(),
            ),
        )
    cutoff = (datetime.now(UTC) - timedelta(days=14)).isoformat()
    conn.execute("DELETE FROM after_hours_snapshot WHERE snapshot_at < ?", (cutoff,))


def load_latest_after_hours_snapshot(conn: sqlite3.Connection, limit: int = 20) -> tuple[str, list[dict[str, Any]]]:
    snap = conn.execute(
        "SELECT snapshot_at FROM after_hours_snapshot ORDER BY snapshot_at DESC LIMIT 1"
    ).fetchone()
    snapshot_at = str(snap[0] or "") if snap and snap[0] else ""
    if not snapshot_at:
        return "", []
    rows = conn.execute(
        """
        SELECT symbol, name, asset_group, price, percent_change, volume, market_cap, market_cap_bucket, session_type, source, updated_at
        FROM after_hours_snapshot
        WHERE snapshot_at = ?
        ORDER BY ABS(COALESCE(percent_change, 0)) DESC
        LIMIT ?
        """,
        (snapshot_at, int(limit)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "symbol": str(r[0] or ""),
                "name": str(r[1] or ""),
                "asset_group": str(r[2] or "stocks"),
                "price": finite_or(r[3], 0),
                "percent_change": to_float(r[4]),
                "volume": finite_or(r[5], 0),
                "market_cap": to_float(r[6]),
                "market_cap_bucket": str(r[7] or "") or None,
                "session_type": str(r[8] or ""),
                "source_provider": str(r[9] or "research_tradingview"),
                "updated_at": str(r[10] or ""),
            }
        )
    return snapshot_at, out


def build_after_hours_snapshot_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [
            ex.submit(fetch_tradingview_after_hours, "post", 300),
            ex.submit(fetch_tradingview_after_hours, "pre", 300),
        ]
        for fut in as_completed(futures):
            try:
                rows.extend(fut.result())
            except Exception:
                continue

    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        current = dedup.get(sym)
        if current is None:
            dedup[sym] = row
            continue
        if abs(float(row.get("percent_change") or 0)) > abs(float(current.get("percent_change") or 0)):
            dedup[sym] = row
    return list(dedup.values())


def _build_market_after_hours() -> dict[str, Any]:
    errors: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    snapshot_at = ""

    try:
        rows = build_after_hours_snapshot_rows()
    except Exception as exc:
        errors["research_fetch"] = str(exc)
        rows = []

    if rows:
        rows = [r for r in rows if r.get("percent_change") is not None]
        rows.sort(key=lambda x: abs(float(x.get("percent_change") or 0)), reverse=True)
        snapshot_at = datetime.now(UTC).replace(second=0, microsecond=0).isoformat()
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                persist_after_hours_snapshot(conn, snapshot_at, rows)
                conn.commit()
            finally:
                conn.close()
    else:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                snapshot_at, rows = load_latest_after_hours_snapshot(conn, limit=40)
            finally:
                conn.close()
        if not rows:
            return {
                "updated_at": now_iso(),
                "provider": "research_tradingview",
                "rows": [],
                "count": 0,
                "errors": errors or {"message": "No live or cached after-hours data available"},
            }
        # Keep stale fallback bounded.
        snap_dt = parse_iso_dt(snapshot_at)
        if snap_dt is not None:
            age_h = (datetime.now(UTC) - snap_dt).total_seconds() / 3600.0
            if age_h > max(1, AFTER_HOURS_SNAPSHOT_MAX_AGE_HOURS):
                errors["snapshot_age"] = f"latest_snapshot_older_than_{AFTER_HOURS_SNAPSHOT_MAX_AGE_HOURS}h"

    top_rows = rows[:20]
    payload = {
        "updated_at": now_iso(),
        "provider": "research_tradingview",
        "snapshot_at": snapshot_at,
        "rows": top_rows,
        "count": len(top_rows),
    }
    if errors:
        payload["errors"] = errors
    return payload


@app.get("/api/market/after-hours")
def market_after_hours(refresh: bool = Query(default=False)) -> dict[str, Any]:
    payload, meta = swr_serve(
        "market:after_hours", _build_market_after_hours, refresh_after_sec=120, force_refresh=refresh
    )
    return {**payload, "cache": meta}


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


def _build_news_headlines(limit: int) -> dict[str, Any]:
    feeds = [
        ("MarketWatch Top Stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("Financial Times", "https://www.ft.com/rss/home/uk"),
        ("NYTimes Business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
        ("Investing.com Stocks", "https://www.investing.com/rss/news_25.rss"),
        ("SEC Press Releases", "https://www.sec.gov/news/pressreleases.rss"),
    ]
    headers = {
        "User-Agent": HONEST_USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    }
    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    sec_top_keywords = {
        "charges",
        "charged",
        "fraud",
        "settlement",
        "penalty",
        "penalties",
        "insider",
        "manipulation",
        "crypto",
        "offering",
        "earnings",
        "disclosure",
        "accounting",
        "enforcement",
        "whistleblower",
        "securities",
    }

    # Fetch all feeds concurrently — sequentially this was the slowest part of
    # the News tab (5 feeds x up to 8s timeout each).
    def _fetch_feed(url: str) -> bytes:
        req = UrlRequest(url, headers=headers)
        with urlopen(req, timeout=8) as resp:
            return resp.read()

    feed_bytes: dict[str, bytes | Exception] = {}
    with ThreadPoolExecutor(max_workers=len(feeds)) as feed_pool:
        feed_futures = {feed_pool.submit(_fetch_feed, url): source for source, url in feeds}
        for feed_future in as_completed(feed_futures):
            src = feed_futures[feed_future]
            try:
                feed_bytes[src] = feed_future.result()
            except Exception as exc:
                feed_bytes[src] = exc

    for source, url in feeds:
        try:
            fetched = feed_bytes.get(source)
            if isinstance(fetched, Exception) or fetched is None:
                raise fetched if isinstance(fetched, Exception) else RuntimeError("feed fetch failed")
            root = ET.fromstring(fetched)
            source_rows: list[dict[str, Any]] = []
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date_raw = (item.findtext("pubDate") or "").strip()
                if not title or not link:
                    continue

                pub_iso = None
                if pub_date_raw:
                    try:
                        dt = parsedate_to_datetime(pub_date_raw)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        pub_iso = dt.astimezone(UTC).isoformat()
                    except Exception:
                        pub_iso = None

                source_rows.append(
                    {
                        "title": title,
                        "url": link,
                        "source": source,
                        "published_at": pub_iso,
                    }
                )

            if source == "SEC Press Releases":
                # Keep only high-impact SEC items and cap list size.
                scored_sec: list[tuple[int, float, dict[str, Any]]] = []
                for r in source_rows:
                    title_l = str(r.get("title") or "").lower()
                    keyword_hits = sum(1 for kw in sec_top_keywords if kw in title_l)
                    ts = 0.0
                    pub = r.get("published_at")
                    if pub:
                        try:
                            ts = datetime.fromisoformat(str(pub).replace("Z", "+00:00")).timestamp()
                        except Exception:
                            ts = 0.0
                    if keyword_hits > 0:
                        scored_sec.append((keyword_hits, ts, r))

                if not scored_sec:
                    # Fallback: keep only the latest 2 SEC rows if no keyword matches.
                    source_rows.sort(key=lambda r: str(r.get("published_at") or ""), reverse=True)
                    rows.extend(source_rows[:2])
                else:
                    scored_sec.sort(key=lambda x: (x[0], x[1]), reverse=True)
                    rows.extend([entry[2] for entry in scored_sec[:4]])
            else:
                rows.extend(source_rows)
        except (URLError, TimeoutError, ET.ParseError, Exception) as exc:
            errors[source] = str(exc)

    def parse_intellectia_ts(value: Any) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            # Epoch seconds (or milliseconds) path.
            if raw.isdigit():
                iv = int(raw)
                if iv > 10_000_000_000:
                    iv = iv // 1000
                return datetime.fromtimestamp(iv, tz=UTC).isoformat()
            # ISO timestamp path.
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).isoformat()
        except Exception:
            return None

    def title_from_text(text: str, fallback: str) -> str:
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        if not cleaned:
            return fallback
        parts = re.split(r"[.!?]\s+", cleaned)
        first = parts[0].strip() if parts else cleaned
        if not first:
            first = cleaned
        return first[:180]

    intellectia_headers = {
        "Accept": "application/json",
        "User-Agent": HONEST_USER_AGENT,
    }
    try:
        flash_payload = fetch_json_url(
            f"{INTELLECTIA_API_BASE}/gateway/v1/news/flash?page=1&page_size=40",
            timeout=10,
            headers=intellectia_headers,
        )
        for row in flash_payload.get("list", []) or []:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("id") or "").strip()
            text = str(row.get("content") or "").strip()
            title = str(row.get("title") or "").strip()
            if not title:
                title = title_from_text(text, "Intellectia Flash Update")
            if not title:
                continue
            rows.append(
                {
                    "title": title,
                    "url": f"https://intellectia.ai/news-wire#flash-{item_id or title[:24]}",
                    "source": "Intellectia Flash",
                    "published_at": parse_intellectia_ts(row.get("timestamp")),
                }
            )
    except Exception as exc:
        errors["Intellectia Flash"] = str(exc)

    try:
        stock_news_payload = fetch_json_url(
            f"{INTELLECTIA_API_BASE}/gateway/v1/news/list?page=1&page_size=30&type=stock",
            timeout=10,
            headers=intellectia_headers,
        )
        for row in stock_news_payload.get("articles", []) or []:
            if not isinstance(row, dict):
                continue
            title = ""
            title_arr = row.get("title")
            if isinstance(title_arr, list):
                for t in title_arr:
                    if isinstance(t, dict) and str(t.get("lang") or "").lower() == "en":
                        title = str(t.get("text") or "").strip()
                        break
                if not title:
                    for t in title_arr:
                        if isinstance(t, dict) and str(t.get("text") or "").strip():
                            title = str(t.get("text") or "").strip()
                            break
            if not title:
                title = str(row.get("headline") or "").strip()
            if not title:
                continue
            seo_title = ""
            if isinstance(title_arr, list):
                for t in title_arr:
                    if isinstance(t, dict) and str(t.get("seo_title") or "").strip():
                        seo_title = str(t.get("seo_title") or "").strip()
                        break
            link = str(row.get("url") or "").strip()
            if not link and seo_title:
                link = f"https://intellectia.ai/news/stock/{seo_title}"
            if not link:
                doc_id = str(row.get("doc_id") or "").strip()
                link = f"https://intellectia.ai/news/stock#{doc_id or title[:24]}"
            rows.append(
                {
                    "title": title,
                    "url": link,
                    "source": "Intellectia Stock News",
                    "published_at": parse_intellectia_ts(
                        row.get("publish_time") or row.get("published_at") or row.get("timestamp")
                    ),
                }
            )
    except Exception as exc:
        errors["Intellectia Stock News"] = str(exc)

    try:
        brief_payload = fetch_json_url(
            f"{INTELLECTIA_API_BASE}/gateway/v1/news/market-brief?page=1&page_size=12",
            timeout=10,
            headers=intellectia_headers,
        )
        for row in brief_payload.get("data", []) or []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            content = str(row.get("content") or "").strip()
            if not title:
                title = title_from_text(content, "Intellectia Market Brief")
            if not title:
                continue
            slug = str(row.get("slug") or "").strip()
            link = str(row.get("url") or row.get("web_link") or "").strip()
            if not link:
                link = f"https://intellectia.ai/news/daily-market-brief#{slug or title[:24]}"
            rows.append(
                {
                    "title": title,
                    "url": link,
                    "source": "Intellectia Market Brief",
                    "published_at": parse_intellectia_ts(
                        row.get("timestamp") or row.get("time") or row.get("published_at")
                    ),
                }
            )
    except Exception as exc:
        errors["Intellectia Market Brief"] = str(exc)

    try:
        monitor_payload = fetch_json_url(
            f"{INTELLECTIA_API_BASE}/gateway/v1/news/monitor-news",
            timeout=10,
            headers={**intellectia_headers, "Content-Type": "application/json"},
            data=json.dumps({"page": 1, "page_size": 30}).encode("utf-8"),
            method="POST",
        )
        monitor_data = monitor_payload.get("data") if isinstance(monitor_payload, dict) else {}
        monitor_rows = monitor_data.get("list") if isinstance(monitor_data, dict) else []
        for row in monitor_rows or []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            content = str(row.get("content") or "").strip()
            if not title:
                title = title_from_text(content, "Intellectia Monitor")
            if not title:
                continue
            item_id = str(row.get("id") or "").strip()
            rows.append(
                {
                    "title": title,
                    "url": f"https://intellectia.ai/news/monitor#{item_id or title[:24]}",
                    "source": "Intellectia Monitor",
                    "published_at": parse_intellectia_ts(
                        row.get("timestamp") or row.get("time") or row.get("published_at")
                    ),
                }
            )
    except Exception as exc:
        errors["Intellectia Monitor"] = str(exc)

    walter_rss_urls = [
        "https://nitter.net/DeItaone/rss",
        "https://nitter.poast.org/DeItaone/rss",
    ]
    walter_loaded = False
    for walter_url in walter_rss_urls:
        try:
            req = UrlRequest(walter_url, headers=headers)
            with urlopen(req, timeout=8) as resp:
                raw_bytes = resp.read()
            if not raw_bytes:
                continue
            root = ET.fromstring(raw_bytes)
            walter_count = 0
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date_raw = (item.findtext("pubDate") or "").strip()
                if not title or not link:
                    continue
                pub_iso = None
                if pub_date_raw:
                    try:
                        dt = parsedate_to_datetime(pub_date_raw)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        pub_iso = dt.astimezone(UTC).isoformat()
                    except Exception:
                        pub_iso = None
                rows.append(
                    {
                        "title": title,
                        "url": link,
                        "source": "Walter Bloomberg (Mirror)",
                        "published_at": pub_iso,
                    }
                )
                walter_count += 1
                if walter_count >= 25:
                    break
            if walter_count > 0:
                walter_loaded = True
                break
        except Exception:
            continue
    if not walter_loaded:
        errors["Walter Bloomberg (Mirror)"] = "No reachable public mirror RSS feed."

    source_priority = {
        "Walter Bloomberg (Mirror)": 96,
        "Intellectia Flash": 90,
        "Intellectia Monitor": 86,
        "Intellectia Market Brief": 84,
        "Intellectia Stock News": 82,
        "SEC Press Releases": 110,
        "MarketWatch Top Stories": 75,
        "Financial Times": 70,
        "NYTimes Business": 60,
        "Investing.com Stocks": 55,
    }

    title_stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
    }

    def canonical_url(url: str) -> str:
        try:
            # Strip tracking params and normalize trailing slash so syndicated links map together.
            base = url.split("#", 1)[0]
            parts = base.split("?", 1)
            path = parts[0].rstrip("/")
            if len(parts) == 1:
                return path
            kept = []
            for qp in parts[1].split("&"):
                if not qp:
                    continue
                k = qp.split("=", 1)[0].lower()
                if k.startswith("utm_") or k in {"guccounter", "guce_referrer", "guce_referrer_sig", "ocid", "cmpid"}:
                    continue
                kept.append(qp)
            if not kept:
                return path
            kept.sort()
            return f"{path}?{'&'.join(kept)}"
        except Exception:
            return url

    def normalized_title(title: str) -> str:
        src = re.sub(r"[^a-z0-9\\s]", " ", title.lower())
        src = re.sub(r"\\s+", " ", src).strip()
        return src

    def title_tokens(title: str) -> set[str]:
        out: set[str] = set()
        for token in normalized_title(title).split(" "):
            if len(token) <= 2:
                continue
            if token in title_stopwords:
                continue
            out.add(token)
        return out

    def title_is_similar(a: str, b: str) -> bool:
        if not a or not b:
            return False
        na = normalized_title(a)
        nb = normalized_title(b)
        if na == nb:
            return True
        ta = title_tokens(a)
        tb = title_tokens(b)
        if not ta or not tb:
            return False
        inter = len(ta.intersection(tb))
        union = len(ta.union(tb))
        jaccard = inter / max(1, union)
        overlap = inter / max(1, min(len(ta), len(tb)))
        # Catch same-story rewrites while avoiding over-merge of loosely related macro headlines.
        return (jaccard >= 0.67 and inter >= 3) or (overlap >= 0.82 and inter >= 4)

    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = canonical_url(str(row.get("url") or ""))
        if key not in dedup:
            row["related_sources"] = [row.get("source")]
            row["related_titles"] = [row.get("title")]
            row["duplicate_count"] = 1
            dedup[key] = row
            continue
        existing = dedup[key]
        existing["duplicate_count"] = int(existing.get("duplicate_count") or 1) + 1
        rel_sources = set(existing.get("related_sources") or [])
        rel_sources.add(row.get("source"))
        existing["related_sources"] = sorted([str(s) for s in rel_sources if s])
        rel_titles = existing.get("related_titles") or []
        if row.get("title") and row.get("title") not in rel_titles:
            rel_titles.append(row.get("title"))
            existing["related_titles"] = rel_titles[:6]

    def priority_score(row: dict[str, Any]) -> tuple[float, float]:
        source = str(row.get("source") or "")
        base = float(source_priority.get(source, 40))

        ts = 0.0
        pub = row.get("published_at")
        if pub:
            try:
                ts = datetime.fromisoformat(str(pub).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0.0

        # Recency bonus: max +25 points at publish-time, decays to 0 after 24h.
        recency_bonus = 0.0
        if ts > 0:
            age_hours = max(0.0, (time.time() - ts) / 3600.0)
            recency_bonus = max(0.0, 25.0 - age_hours)

        return base + recency_bonus, ts

    ranked_rows = list(dedup.values())
    ranked_rows.sort(key=lambda row: priority_score(row), reverse=True)
    consolidated: list[dict[str, Any]] = []
    for row in ranked_rows:
        merged = False
        for kept in consolidated:
            if title_is_similar(str(row.get("title") or ""), str(kept.get("title") or "")):
                kept["duplicate_count"] = int(kept.get("duplicate_count") or 1) + int(row.get("duplicate_count") or 1)
                rel_sources = set(kept.get("related_sources") or [])
                rel_sources.update(row.get("related_sources") or [row.get("source")])
                kept["related_sources"] = sorted([str(s) for s in rel_sources if s])
                rel_titles = kept.get("related_titles") or []
                for t in row.get("related_titles") or [row.get("title")]:
                    if t and t not in rel_titles:
                        rel_titles.append(t)
                kept["related_titles"] = rel_titles[:6]
                merged = True
                break
        if not merged:
            consolidated.append(row)
        if len(consolidated) >= limit:
            break

    sorted_rows = consolidated[:limit]

    payload: dict[str, Any] = {
        "updated_at": now_iso(),
        "count": len(sorted_rows),
        "rows": sorted_rows,
    }
    if errors:
        payload["errors"] = errors
    return payload


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


def run_social_sentiment_scan(
    limit: int = 100,
    experiment_id: str | None = None,
) -> dict[str, Any]:
    """The heavy live scan (Reddit + Stocktwits + ApeWisdom with pacing).

    Can take 30-60s of wall-clock network time, so it is only ever called from
    the background refresher in the /api/social/sentiment endpoint below —
    never directly on a request path.
    """
    exp = (experiment_id or "").strip()
    cache_key = f"social:sentiment:{limit}:{exp}"
    cached = get_cached(cache_key, 120)
    if cached is not None:
        return cached

    symbol_master, symbol_master_snapshot_date, symbol_master_source = load_symbol_master()
    symbol_master_set = set(symbol_master.keys())

    now_ts = time.time()
    six_hours_ago = now_ts - (6 * 3600)
    twelve_hours_ago = now_ts - (12 * 3600)

    mentions: list[dict[str, Any]] = []
    fetch_errors: dict[str, str] = {}
    source_counts: dict[str, int] = {}
    source_health: dict[str, dict[str, Any]] = {}
    tiktok_enabled = bool(TIKTOK_PROVIDER_URL and TIKTOK_PROVIDER_API_KEY)
    scan_started_at = time.time()
    scan_budget_hit = False

    def within_scan_budget() -> bool:
        return (time.time() - scan_started_at) < max(5.0, SOCIAL_SCAN_TIME_BUDGET_SEC)

    def source_attempt(key: str) -> None:
        slot = source_health.setdefault(
            key,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "rows": 0,
                "retry_count": 0,
                "skipped_count": 0,
                "total_elapsed_ms": 0,
                "max_elapsed_ms": 0,
                "last_origin": None,
                "cooldown_remaining_sec": 0,
                "last_error": None,
            },
        )
        slot["attempts"] += 1

    def source_success(
        key: str,
        rows_count: int,
        elapsed_ms: int = 0,
        retries_used: int = 0,
        origin: str | None = None,
    ) -> None:
        slot = source_health.setdefault(
            key,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "rows": 0,
                "retry_count": 0,
                "skipped_count": 0,
                "total_elapsed_ms": 0,
                "max_elapsed_ms": 0,
                "last_origin": None,
                "cooldown_remaining_sec": 0,
                "last_error": None,
            },
        )
        slot["successes"] += 1
        slot["rows"] += int(rows_count)
        slot["retry_count"] += int(retries_used)
        slot["total_elapsed_ms"] += int(elapsed_ms)
        slot["max_elapsed_ms"] = max(int(slot.get("max_elapsed_ms") or 0), int(elapsed_ms))
        if origin:
            slot["last_origin"] = origin
        slot["cooldown_remaining_sec"] = source_cooldown_remaining_sec(key)
        source_record_success(key)

    def source_failure(key: str, err: Exception, elapsed_ms: int = 0, retries_used: int = 0) -> None:
        slot = source_health.setdefault(
            key,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "rows": 0,
                "retry_count": 0,
                "skipped_count": 0,
                "total_elapsed_ms": 0,
                "max_elapsed_ms": 0,
                "last_origin": None,
                "cooldown_remaining_sec": 0,
                "last_error": None,
            },
        )
        slot["failures"] += 1
        slot["retry_count"] += int(retries_used)
        slot["total_elapsed_ms"] += int(elapsed_ms)
        slot["max_elapsed_ms"] = max(int(slot.get("max_elapsed_ms") or 0), int(elapsed_ms))
        _failures, cooldown = source_record_failure(key)
        slot["cooldown_remaining_sec"] = int(cooldown)
        slot["last_error"] = str(err)

    def source_skipped(key: str, reason: str) -> None:
        slot = source_health.setdefault(
            key,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "rows": 0,
                "retry_count": 0,
                "skipped_count": 0,
                "total_elapsed_ms": 0,
                "max_elapsed_ms": 0,
                "last_origin": None,
                "cooldown_remaining_sec": 0,
                "last_error": None,
            },
        )
        slot["skipped_count"] += 1
        slot["cooldown_remaining_sec"] = source_cooldown_remaining_sec(key)
        slot["last_error"] = reason

    fallback_rows_count = 0
    if within_scan_budget():
        source_key = "apewisdom:wallstreetbets"
        source_attempt(source_key)
        started = time.time()
        try:
            ape_rows = fetch_apewisdom_wallstreetbets(limit=APEWISDOM_MAX_RESULTS)
            source_success(source_key, len(ape_rows), elapsed_ms=int((time.time() - started) * 1000), origin="apewisdom_api")
            if ape_rows:
                append_mentions_from_rows(mentions, ape_rows)
                source_counts[source_key] = len(ape_rows)
                fallback_rows_count += len(ape_rows)
        except Exception as exc:
            source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000))
            fetch_errors[source_key] = str(exc)
    else:
        scan_budget_hit = True

    if within_scan_budget():
        source_key = "stocktwits:trending"
        source_attempt(source_key)
        started = time.time()
        try:
            trend_rows = fetch_stocktwits_trending(limit=40)
            source_success(
                source_key,
                len(trend_rows),
                elapsed_ms=int((time.time() - started) * 1000),
                origin="stocktwits_trending_api",
            )
            if trend_rows:
                append_mentions_from_rows(mentions, trend_rows)
                source_counts[source_key] = len(trend_rows)
                fallback_rows_count += len(trend_rows)
        except Exception as exc:
            source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000))
            fetch_errors[source_key] = str(exc)
    else:
        scan_budget_hit = True

    reddit_fast_fail = fallback_rows_count > 0 and SOCIAL_SKIP_REDDIT_IF_FALLBACK_READY
    reddit_failure_streak = 0
    for subreddit in SOCIAL_SUBREDDITS:
        if reddit_fast_fail:
            source_skipped(f"reddit:{subreddit}:hot", "fast_fail_active")
            source_skipped(f"reddit:{subreddit}:new", "fast_fail_active")
            source_skipped(f"reddit:{subreddit}:old", "fast_fail_active")
            continue
        if not within_scan_budget():
            scan_budget_hit = True
            break
        merged: dict[str, dict[str, Any]] = {}
        for sort in ("hot", "new"):
            if not within_scan_budget():
                scan_budget_hit = True
                break
            source_key = f"reddit:{subreddit}:{sort}"
            if source_cooldown_remaining_sec(source_key) > 0:
                source_skipped(source_key, "cooldown_active")
                continue
            source_attempt(source_key)
            started = time.time()
            try:
                rows, fetch_meta = parse_reddit_listing(subreddit, sort, 50)
                source_success(
                    source_key,
                    len(rows),
                    elapsed_ms=int(fetch_meta.get("elapsed_ms") or ((time.time() - started) * 1000)),
                    retries_used=int(fetch_meta.get("retries_used") or 0),
                    origin=str(fetch_meta.get("origin") or "reddit_json_public"),
                )
                if rows:
                    source_counts[f"reddit:{subreddit}:{rows[0].get('origin', 'reddit_unknown')}"] = (
                        source_counts.get(f"reddit:{subreddit}:{rows[0].get('origin', 'reddit_unknown')}", 0) + len(rows)
                    )
                for row in rows:
                    key = row.get("url") or f"{row.get('title')}|{row.get('author')}"
                    merged[key] = row
            except Exception as exc:
                source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000), retries_used=max(0, REDDIT_FETCH_RETRIES - 1))
                fetch_errors[source_key] = str(exc)
                reddit_failure_streak += 1
                err_lower = str(exc).lower()
                if reddit_failure_streak >= 2 and ("403" in err_lower or "timed out" in err_lower or "blocked" in err_lower):
                    reddit_fast_fail = True

        rows = list(merged.values())
        if rows:
            reddit_failure_streak = 0
            append_mentions_from_rows(mentions, rows)
            source_counts[f"reddit:{subreddit}:merged"] = len(rows)
            continue

        if not within_scan_budget():
            scan_budget_hit = True
            break
        source_key = f"reddit:{subreddit}:old"
        if source_cooldown_remaining_sec(source_key) > 0:
            source_skipped(source_key, "cooldown_active")
            continue
        source_attempt(source_key)
        started = time.time()
        try:
            rows_old = parse_old_reddit(subreddit, 35)
            source_success(source_key, len(rows_old), elapsed_ms=int((time.time() - started) * 1000), origin="old_reddit_html")
            append_mentions_from_rows(mentions, rows_old)
            source_counts[source_key] = len(rows_old)
        except Exception as exc:
            source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000))
            fetch_errors[source_key] = str(exc)

    by_ticker: dict[str, dict[str, Any]] = {}
    for mention in mentions:
        ticker = str(mention.get("ticker") or "").upper()
        if not ticker or ticker in TICKER_EXCLUDE:
            continue
        if ticker not in symbol_master_set:
            continue

        mention_weight = max(1, int(mention.get("mention_weight") or 1))
        source_name = str(mention.get("source") or "unknown")
        if source_name.startswith("reddit:"):
            source_family = "reddit"
        elif source_name.startswith("stocktwits"):
            source_family = "stocktwits"
        elif source_name.startswith("apewisdom"):
            source_family = "apewisdom"
        else:
            source_family = source_name
        text = str(mention.get("text") or "")
        context_score, cashtag_mentions = mention_context_score(text, ticker)

        slot = by_ticker.setdefault(
            ticker,
            {
                "ticker": ticker,
                "raw_mentions": 0,
                "qualified_mentions": 0,
                "authors": set(),
                "threads": set(),
                "engagement": 0,
                "max_engagement": 0,
                "bullish": 0,
                "bearish": 0,
                "neutral": 0,
                "mentions_6h": 0,
                "mentions_prev_6h": 0,
                "mentions_1h": 0,
                "authors_1h": set(),
                "engagement_1h": 0,
                "cashtag_mentions": 0,
                "context_sum": 0.0,
                "context_hits": 0,
                "drivers": {},
                "sources": set(),  # source families
                "source_detail": set(),  # specific endpoints
                "source_mentions": {},
                "catalyst_hits": {},
                "options_context_hits": 0,
                "validation_status": "pending",
            },
        )

        slot["raw_mentions"] += mention_weight
        if context_score >= 0.25 or cashtag_mentions > 0:
            slot["qualified_mentions"] += mention_weight
        slot["authors"].add(str(mention.get("author") or "unknown"))
        slot["threads"].add(str(mention.get("url") or ""))
        slot["engagement"] += int(mention.get("engagement") or 0) + max(0, mention_weight - 1)
        slot["max_engagement"] = max(slot["max_engagement"], int(mention.get("engagement") or 0))
        slot["cashtag_mentions"] += (cashtag_mentions * mention_weight)
        slot["context_sum"] += (context_score * mention_weight)
        slot["context_hits"] += mention_weight
        slot["sources"].add(source_family)
        slot["source_detail"].add(source_name)
        slot["source_mentions"][source_family] = int(slot["source_mentions"].get(source_family) or 0) + mention_weight
        for cat, cnt in extract_catalyst_hits(text).items():
            slot["catalyst_hits"][cat] = int(slot["catalyst_hits"].get(cat) or 0) + int(cnt) * mention_weight
        slot["options_context_hits"] += options_context_hits(text) * mention_weight

        s = str(mention.get("sentiment") or "neutral")
        if s == "bullish":
            slot["bullish"] += mention_weight
        elif s == "bearish":
            slot["bearish"] += mention_weight
        else:
            slot["neutral"] += mention_weight

        created_utc = float(mention.get("created_utc") or 0)
        if created_utc >= (now_ts - 3600):
            slot["mentions_1h"] += mention_weight
            slot["authors_1h"].add(str(mention.get("author") or "unknown"))
            slot["engagement_1h"] += int(mention.get("engagement") or 0) + max(0, mention_weight - 1)
        if created_utc >= six_hours_ago:
            slot["mentions_6h"] += mention_weight
        elif created_utc >= twelve_hours_ago:
            slot["mentions_prev_6h"] += mention_weight

        for word in SENTIMENT_BULL + SENTIMENT_BEAR:
            if word in text.lower():
                slot["drivers"][word] = int(slot["drivers"].get(word) or 0) + 1

    prequalified: list[str] = []
    for ticker, row in by_ticker.items():
        unique_authors = len(row["authors"])
        cross_source = len(row["sources"]) >= 2
        has_min_evidence = (
            (row["raw_mentions"] >= 2 and unique_authors >= 2)
            or row["cashtag_mentions"] >= 1
            or row["max_engagement"] >= 120
            or cross_source
        )
        if row["qualified_mentions"] <= 0:
            continue
        if not has_min_evidence:
            continue
        if ticker in AMBIGUOUS_TICKERS or len(ticker) <= 2:
            strong_for_ambiguous = (
                row["cashtag_mentions"] >= 1
                or (unique_authors >= 3 and row["qualified_mentions"] >= 2)
                or cross_source
            )
            if not strong_for_ambiguous:
                continue
        prequalified.append(ticker)

    top_tickers = sorted(prequalified, key=lambda t: by_ticker[t]["raw_mentions"], reverse=True)[:25]

    if tiktok_enabled and top_tickers and within_scan_budget():
        source_key = "tiktok:provider"
        if source_cooldown_remaining_sec(source_key) > 0:
            source_skipped(source_key, "cooldown_active")
        else:
            source_attempt(source_key)
            started = time.time()
            try:
                tiktok_rows = fetch_tiktok_messages(top_tickers, 200)
                source_success(source_key, len(tiktok_rows), elapsed_ms=int((time.time() - started) * 1000), origin="tiktok_provider")
                source_counts[source_key] = len(tiktok_rows)
                for msg in tiktok_rows:
                    for sym in msg.get("tickers") or []:
                        sym_u = str(sym).upper()
                        if sym_u not in by_ticker:
                            continue
                        slot = by_ticker[sym_u]
                        text = str(msg.get("text") or "")
                        context_score, cashtag_mentions = mention_context_score(text, sym_u)
                        slot["raw_mentions"] += 1
                        if context_score >= 0.25 or cashtag_mentions > 0:
                            slot["qualified_mentions"] += 1
                        slot["authors"].add(str(msg.get("author") or "unknown"))
                        slot["engagement"] += int(msg.get("engagement") or 0)
                        slot["max_engagement"] = max(slot["max_engagement"], int(msg.get("engagement") or 0))
                        slot["cashtag_mentions"] += cashtag_mentions
                        slot["context_sum"] += context_score
                        slot["context_hits"] += 1
                        slot["sources"].add("tiktok")
                        slot["source_detail"].add("tiktok")
                        slot["source_mentions"]["tiktok"] = int(slot["source_mentions"].get("tiktok") or 0) + 1
                        for cat, cnt in extract_catalyst_hits(text).items():
                            slot["catalyst_hits"][cat] = int(slot["catalyst_hits"].get(cat) or 0) + int(cnt)
                        slot["options_context_hits"] += options_context_hits(text)
                        sentiment = str(msg.get("sentiment") or "neutral")
                        if sentiment == "bullish":
                            slot["bullish"] += 1
                        elif sentiment == "bearish":
                            slot["bearish"] += 1
                        else:
                            slot["neutral"] += 1
                        created_utc = float(msg.get("created_utc") or 0)
                        if created_utc >= (now_ts - 3600):
                            slot["mentions_1h"] += 1
                            slot["authors_1h"].add(str(msg.get("author") or "unknown"))
                            slot["engagement_1h"] += int(msg.get("engagement") or 0)
                        if created_utc >= six_hours_ago:
                            slot["mentions_6h"] += 1
                        elif created_utc >= twelve_hours_ago:
                            slot["mentions_prev_6h"] += 1
            except Exception as exc:
                source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000))
                fetch_errors[source_key] = str(exc)

    stocktwits_budget = max(1, SOCIAL_MAX_STOCKTWITS_SYMBOLS)
    stocktwits_targets = top_tickers[:stocktwits_budget]
    for sym in stocktwits_targets:
        if not within_scan_budget():
            scan_budget_hit = True
            break
        try:
            twits = fetch_stocktwits_messages(sym)
            source_counts[f"stocktwits:{sym}"] = len(twits)
            for msg in twits:
                slot = by_ticker[sym]
                text = str(msg.get("text") or "")
                context_score, cashtag_mentions = mention_context_score(text, sym)
                slot["raw_mentions"] += 1
                if context_score >= 0.25 or cashtag_mentions > 0:
                    slot["qualified_mentions"] += 1
                slot["authors"].add(str(msg.get("author") or "unknown"))
                slot["engagement"] += int(msg.get("engagement") or 0)
                slot["max_engagement"] = max(slot["max_engagement"], int(msg.get("engagement") or 0))
                slot["cashtag_mentions"] += cashtag_mentions
                slot["context_sum"] += context_score
                slot["context_hits"] += 1
                slot["sources"].add("stocktwits")
                slot["source_detail"].add("stocktwits")
                slot["source_mentions"]["stocktwits"] = int(slot["source_mentions"].get("stocktwits") or 0) + 1
                for cat, cnt in extract_catalyst_hits(text).items():
                    slot["catalyst_hits"][cat] = int(slot["catalyst_hits"].get(cat) or 0) + int(cnt)
                slot["options_context_hits"] += options_context_hits(text)
                sentiment = str(msg.get("sentiment") or "neutral")
                if sentiment == "bullish":
                    slot["bullish"] += 1
                elif sentiment == "bearish":
                    slot["bearish"] += 1
                else:
                    slot["neutral"] += 1
                created_utc = float(msg.get("created_utc") or 0)
                if created_utc >= (now_ts - 3600):
                    slot["mentions_1h"] += 1
                    slot["authors_1h"].add(str(msg.get("author") or "unknown"))
                    slot["engagement_1h"] += int(msg.get("engagement") or 0)
                if created_utc >= six_hours_ago:
                    slot["mentions_6h"] += 1
                elif created_utc >= twelve_hours_ago:
                    slot["mentions_prev_6h"] += 1
        except Exception as exc:
            fetch_errors[f"stocktwits:{sym}"] = str(exc)
        time.sleep(0.25)

    if scan_budget_hit:
        fetch_errors["scan_budget"] = (
            f"Scan exceeded {max(5.0, SOCIAL_SCAN_TIME_BUDGET_SEC):.1f}s budget; returned partial results."
        )

    current_bucket = int(now_ts // 3600)
    baseline_rows: dict[str, dict[str, float]] = {}
    for ticker, row in by_ticker.items():
        baseline_rows[ticker] = {
            "mentions": float(int(row["mentions_1h"])),
            "unique_authors": float(len(row["authors_1h"])),
            "engagement": float(int(row["engagement_1h"])),
        }
    upsert_social_baseline(current_bucket, baseline_rows)
    baseline = social_baseline_stats(list(by_ticker.keys()), now_ts)

    # Phase 3: tiered candidate quality shortlist for validation.
    tier_candidates: list[dict[str, Any]] = []
    for ticker, row in by_ticker.items():
        raw_mentions = int(row["raw_mentions"])
        if raw_mentions <= 0:
            continue
        unique_authors = len(row["authors"])
        qualified_mentions = int(row["qualified_mentions"])
        avg_context = float(row["context_sum"]) / max(1, int(row["context_hits"]))
        source_mentions = dict(row.get("source_mentions") or {})
        weighted_source_score = weighted_source_signal(source_mentions)
        cross_source_count = len(source_mentions)
        cross_source = cross_source_count >= 2
        ambiguous = ticker in AMBIGUOUS_TICKERS or len(ticker) <= 2

        confidence = (
            1.0  # symbol master match
            + min(1.5, int(row["cashtag_mentions"]) * 0.35)
            + avg_context * 1.2
            + (0.6 if unique_authors >= 2 else 0.0)
            + min(0.9, weighted_source_score * 0.25)
            + (0.4 if cross_source else 0.0)
        )
        quality_score, author_diversity = compute_mention_quality(
            raw_mentions=raw_mentions,
            qualified_mentions=qualified_mentions,
            unique_authors=unique_authors,
            cashtag_mentions=int(row["cashtag_mentions"]),
            avg_context=avg_context,
            cross_source_count=cross_source_count,
            weighted_source_score=weighted_source_score,
            ambiguous=ambiguous,
        )
        confidence = confidence + quality_score - (0.7 if ambiguous else 0.0) - (0.5 if author_diversity < 0.35 else 0.0)

        minimum_evidence = (
            (raw_mentions >= 2 and unique_authors >= 2)
            or int(row["cashtag_mentions"]) >= 1
            or int(row["max_engagement"]) >= 120
            or cross_source
        )
        if qualified_mentions <= 0 or not minimum_evidence:
            continue
        if confidence < 1.2:
            continue
        tier_candidates.append(
            {
                "ticker": ticker,
                "confidence": confidence,
                "quality_score": quality_score,
                "tier": confidence_tier_from_score(confidence),
                "weighted_source_score": weighted_source_score,
            }
        )

    tier_candidates.sort(key=lambda x: float(x["confidence"]), reverse=True)
    tier_index = {str(c["ticker"]): c for c in tier_candidates}
    to_validate = [str(c["ticker"]) for c in tier_candidates if str(c["tier"]) in {"A", "B"}][:120]
    if SOCIAL_QUOTE_VALIDATION_ENABLED:
        valid_tickers, validation_diag = filter_valid_symbols(to_validate)
        validation_mode = "quote_validation"
        if validation_diag.get("provider_unavailable") and to_validate:
            # Degraded mode: keep high-confidence, symbol-master-qualified set when quote provider is unavailable.
            valid_tickers = set(to_validate)
            validation_mode = "confidence_fallback"
    else:
        valid_tickers = set(to_validate)
        validation_mode = "confidence_only"
        validation_diag = {
            "provider_unavailable": False,
            "auth_error_count": 0,
            "errors": [],
            "quote_provider_cooldown_sec": quote_provider_cooldown_remaining_sec(),
        }

    market_regime = "unknown"
    try:
        spy_hist = fetch_history("SPY", "1Y", ttl=180)
        spy_ind = compute_indicators(spy_hist)
        spy_last = spy_ind.iloc[-1]
        spy_close = to_float(spy_last.get("close")) or 0.0
        spy_sma200 = to_float(spy_last.get("sma200")) or 0.0
        spy_macd = to_float(spy_last.get("macd")) or 0.0
        spy_signal = to_float(spy_last.get("macd_signal")) or 0.0
        if spy_close > spy_sma200 and spy_macd >= spy_signal:
            market_regime = "risk_on"
        elif spy_close < spy_sma200 and spy_macd < spy_signal:
            market_regime = "risk_off"
        else:
            market_regime = "mixed"
    except Exception:
        market_regime = "unknown"

    tradability_profiles: dict[str, dict[str, Any]] = {}
    quote_map: dict[str, dict[str, Any]] = {}
    valid_list = sorted(list(valid_tickers))
    quote_provider_blocked = bool(validation_diag.get("provider_unavailable")) or quote_provider_cooldown_remaining_sec() > 0
    if valid_list and not quote_provider_blocked:
        try:
            quote_df = fetch_quote(valid_list)
            for _, q in quote_df.iterrows():
                sym = str(q.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                quote_map[sym] = {
                    "last_price": to_float(q.get("last_price")) or to_float(q.get("price")) or 0.0,
                    "volume": to_float(q.get("volume")) or 0.0,
                    "market_cap": to_float(q.get("market_cap")),
                    "bid": to_float(q.get("bid")) or to_float(q.get("bid_price")),
                    "ask": to_float(q.get("ask")) or to_float(q.get("ask_price")),
                }
        except Exception as exc:
            if is_quote_provider_auth_error(exc):
                quote_provider_record_auth_failure()
            quote_map = {}

    volatility_candidates = {str(c["ticker"]) for c in tier_candidates[:40]}
    for ticker in valid_list:
        q = quote_map.get(ticker, {})
        last_price = float(q.get("last_price") or 0.0)
        volume = float(q.get("volume") or 0.0)
        market_cap = q.get("market_cap")
        spread_bps: float | None = None
        bid = q.get("bid")
        ask = q.get("ask")
        if bid is not None and ask is not None and last_price > 0 and ask >= bid > 0:
            spread_bps = ((float(ask) - float(bid)) / last_price) * 10000.0

        annualized_vol_pct: float | None = None
        if ticker in volatility_candidates:
            try:
                hist = fetch_history(ticker, "6M", ttl=180)
                returns = hist["close"].pct_change().dropna()
                if not returns.empty:
                    annualized_vol_pct = float(returns.std() * math.sqrt(252) * 100.0)
            except Exception:
                annualized_vol_pct = None

        liquidity_score = min(1.0, math.log10(max(1.0, volume)) / 7.0)
        spread_penalty = 0.0 if spread_bps is None else min(0.65, max(0.0, (spread_bps - 15.0) / 120.0))
        vol_penalty = 0.0 if annualized_vol_pct is None else min(0.55, max(0.0, (annualized_vol_pct - 55.0) / 120.0))
        mc_bucket = market_cap_bucket(market_cap)
        cap_multiplier = float(MARKET_CAP_MULTIPLIER.get(str(mc_bucket), 0.82))
        regime_multiplier = 1.04 if market_regime == "risk_on" else (0.92 if market_regime == "risk_off" else 1.0)

        tradability_score = max(
            0.1,
            (0.35 + liquidity_score * 0.95 - spread_penalty - vol_penalty) * cap_multiplier * regime_multiplier,
        )
        gate_reasons: list[str] = []
        if volume > 0 and volume < 300_000:
            gate_reasons.append("low_volume")
        if spread_bps is not None and spread_bps > 80:
            gate_reasons.append("wide_spread")
        if annualized_vol_pct is not None and annualized_vol_pct > 130:
            gate_reasons.append("extreme_volatility")
        gate_passed = len(gate_reasons) == 0

        tradability_profiles[ticker] = {
            "score": round(tradability_score, 4),
            "gate_passed": gate_passed,
            "gate_reasons": gate_reasons,
            "liquidity_score": round(liquidity_score, 4),
            "spread_bps": round(spread_bps, 3) if spread_bps is not None else None,
            "annualized_volatility_pct": round(annualized_vol_pct, 3) if annualized_vol_pct is not None else None,
            "market_cap": market_cap,
            "market_cap_bucket": mc_bucket,
            "regime": market_regime,
        }

    ranked = []
    for ticker, row in by_ticker.items():
        raw_mentions = int(row["raw_mentions"])
        if raw_mentions <= 0:
            continue
        unique_authors = len(row["authors"])
        qualified_mentions = int(row["qualified_mentions"])
        bullish = int(row["bullish"])
        bearish = int(row["bearish"])
        neutral = int(row["neutral"])
        mentions_6h = int(row["mentions_6h"])
        mentions_prev_6h = int(row["mentions_prev_6h"])
        mentions_1h = int(row["mentions_1h"])
        unique_authors_1h = len(row["authors_1h"])
        engagement_1h = int(row["engagement_1h"])
        momentum_ratio = mentions_6h / max(1, mentions_prev_6h)
        sentiment_score = (bullish - bearish) / max(1, raw_mentions)
        avg_context = float(row["context_sum"]) / max(1, int(row["context_hits"]))
        cross_source = len(row["sources"]) >= 2
        ambiguity_risk = "high" if (ticker in AMBIGUOUS_TICKERS or len(ticker) <= 2) else "low"
        validation_status = "passed" if ticker in valid_tickers else "failed"
        if validation_status != "passed":
            continue

        b = baseline.get(ticker, {})
        avg_mentions_1h_30d = float(b.get("avg_mentions_1h_30d") or 0.0)
        avg_unique_authors_1h_30d = float(b.get("avg_unique_authors_1h_30d") or 0.0)
        avg_engagement_1h_30d = float(b.get("avg_engagement_1h_30d") or 0.0)
        std_engagement_1h_30d = float(b.get("std_engagement_1h_30d") or 0.0)
        mention_buzz_ratio = mentions_1h / max(1.0, avg_mentions_1h_30d)
        author_buzz_ratio = unique_authors_1h / max(1.0, avg_unique_authors_1h_30d)
        engagement_zscore = (engagement_1h - avg_engagement_1h_30d) / (std_engagement_1h_30d or 1.0)

        bullish = int(row["bullish"])
        bearish = int(row["bearish"])
        sentiment_label = "neutral"
        if sentiment_score >= 0.2:
            sentiment_label = "bullish"
        elif sentiment_score <= -0.2:
            sentiment_label = "bearish"

        source_mentions = dict(row.get("source_mentions") or {})
        weighted_source_score = weighted_source_signal(source_mentions)
        cross_source_count = len(source_mentions)
        candidate_info = tier_index.get(ticker)
        if candidate_info is None:
            quality_score, author_diversity = compute_mention_quality(
                raw_mentions=raw_mentions,
                qualified_mentions=qualified_mentions,
                unique_authors=unique_authors,
                cashtag_mentions=int(row["cashtag_mentions"]),
                avg_context=avg_context,
                cross_source_count=cross_source_count,
                weighted_source_score=weighted_source_score,
                ambiguous=ambiguity_risk == "high",
            )
            candidate_confidence = (
                1.0
                + min(1.5, int(row["cashtag_mentions"]) * 0.35)
                + avg_context * 1.2
                + (0.6 if unique_authors >= 2 else 0.0)
                + min(0.9, weighted_source_score * 0.25)
                + (0.4 if cross_source else 0.0)
                + quality_score
                - (0.7 if ambiguity_risk == "high" else 0.0)
                - (0.5 if author_diversity < 0.35 else 0.0)
            )
            candidate_tier = confidence_tier_from_score(candidate_confidence)
        else:
            candidate_confidence = float(candidate_info["confidence"])
            quality_score = float(candidate_info["quality_score"])
            candidate_tier = str(candidate_info["tier"])
            author_diversity = unique_authors / max(1, raw_mentions)

        tier_bonus = 1.2 if candidate_tier == "A" else (0.4 if candidate_tier == "B" else -0.3)
        weighted_source_bonus = min(3.0, weighted_source_score * 1.05)
        tradability = tradability_profiles.get(ticker) or {
            "score": 0.75,
            "gate_passed": True,
            "gate_reasons": [],
            "liquidity_score": 0.5,
            "spread_bps": None,
            "annualized_volatility_pct": None,
            "market_cap": None,
            "market_cap_bucket": None,
            "regime": market_regime,
        }
        tradability_score = float(tradability.get("score") or 0.75)
        tradability_penalty = 0.0 if bool(tradability.get("gate_passed")) else 4.5
        catalyst_pairs = sorted((row.get("catalyst_hits") or {}).items(), key=lambda kv: kv[1], reverse=True)
        catalyst_tags = [str(k) for k, _v in catalyst_pairs[:3]]
        options_intensity = int(row.get("options_context_hits") or 0)
        options_context_score = min(1.0, options_intensity / max(1.0, raw_mentions * 1.2))

        rank_score = (
            qualified_mentions * 1.4
            + unique_authors * 0.8
            + float(row["engagement"]) / 250.0
            + max(0.0, momentum_ratio - 1.0) * 8.0
            + max(0.0, mention_buzz_ratio - 1.0) * 5.0
            + max(0.0, author_buzz_ratio - 1.0) * 4.0
            + max(-2.0, min(5.0, engagement_zscore)) * 1.8
            + sentiment_score * 10.0
            + candidate_confidence * 3.0
            + quality_score * 7.5
            + weighted_source_bonus
            + tier_bonus
            + tradability_score * 4.0
            + options_context_score * 1.6
            - tradability_penalty
        )

        driver_pairs = sorted(row["drivers"].items(), key=lambda kv: kv[1], reverse=True)
        drivers = [k for k, _ in driver_pairs[:4]]
        explainability = {
            "top_positive_factors": [
                f"tier:{candidate_tier}",
                f"quality:{quality_score:.3f}",
                f"weighted_sources:{weighted_source_score:.3f}",
                f"buzz_ratio:{mention_buzz_ratio:.2f}x",
            ],
            "penalties": [
                f"ambiguity:{ambiguity_risk}",
                f"author_diversity:{author_diversity:.3f}",
                f"tradability_penalty:{tradability_penalty:.2f}",
            ],
            "source_mentions": source_mentions,
        }

        ranked.append(
            {
                "ticker": ticker,
                "mentions": raw_mentions,
                "raw_mentions": raw_mentions,
                "qualified_mentions": qualified_mentions,
                "unique_authors": unique_authors,
                "cashtag_mentions": int(row["cashtag_mentions"]),
                "cross_source_confirmed": cross_source,
                "finance_context_score": round(avg_context, 4),
                "ambiguity_risk": ambiguity_risk,
                "validation_status": validation_status,
                "engagement": int(row["engagement"]),
                "bullish": bullish,
                "bearish": bearish,
                "neutral": neutral,
                "bullish_pct": round((bullish / max(1, raw_mentions)) * 100, 2),
                "bearish_pct": round((bearish / max(1, raw_mentions)) * 100, 2),
                "sentiment_score": round(sentiment_score, 4),
                "sentiment_label": sentiment_label,
                "mentions_6h": mentions_6h,
                "mentions_prev_6h": mentions_prev_6h,
                "momentum_ratio": round(momentum_ratio, 3),
                "current_mentions_1h": mentions_1h,
                "avg_mentions_1h_30d": round(avg_mentions_1h_30d, 4),
                "mention_buzz_ratio": round(mention_buzz_ratio, 4),
                "current_unique_authors_1h": unique_authors_1h,
                "avg_unique_authors_1h_30d": round(avg_unique_authors_1h_30d, 4),
                "author_buzz_ratio": round(author_buzz_ratio, 4),
                "engagement_zscore": round(engagement_zscore, 4),
                "baseline_samples_30d": int(b.get("samples") or 0),
                "candidate_confidence": round(candidate_confidence, 4),
                "candidate_tier": candidate_tier,
                "mention_quality_score": round(quality_score, 4),
                "weighted_source_score": round(weighted_source_score, 4),
                "tradability_score": round(tradability_score, 4),
                "tradability_gate_passed": bool(tradability.get("gate_passed")),
                "tradability_gate_reasons": tradability.get("gate_reasons") or [],
                "spread_bps": tradability.get("spread_bps"),
                "annualized_volatility_pct": tradability.get("annualized_volatility_pct"),
                "market_cap": tradability.get("market_cap"),
                "market_cap_bucket": tradability.get("market_cap_bucket"),
                "market_regime": tradability.get("regime"),
                "catalyst_tags": catalyst_tags,
                "options_context_score": round(options_context_score, 4),
                "options_context_hits": options_intensity,
                "rank_score": round(rank_score, 3),
                "sources": sorted(list(row["sources"])),
                "why": drivers,
                "explainability": explainability,
                "model_id": SCORING_MODEL_ID,
                "asset_type": symbol_master.get(ticker, {}).get("asset_type", "equity"),
                "exchange": symbol_master.get(ticker, {}).get("exchange", ""),
            }
        )

    ranked.sort(key=lambda x: (x["rank_score"], x["qualified_mentions"], x["engagement"]), reverse=True)
    ranked = ranked[:limit]
    tier_counts = {
        "A": sum(1 for row in ranked if str(row.get("candidate_tier")) == "A"),
        "B": sum(1 for row in ranked if str(row.get("candidate_tier")) == "B"),
        "C": sum(1 for row in ranked if str(row.get("candidate_tier")) == "C"),
    }
    tradability_gate_failed_count = sum(1 for row in ranked if not bool(row.get("tradability_gate_passed")))
    signal_run_id = ""
    try:
        signal_run_id = persist_social_signal_run(
            ranked,
            model_id=SCORING_MODEL_ID,
            market_regime=market_regime,
            experiment_id=exp or None,
        )
    except Exception:
        signal_run_id = ""

    payload = {
        "updated_at": now_iso(),
        "count": len(ranked),
        "rows": ranked,
        "model_id": SCORING_MODEL_ID,
        "experiment_id": exp or None,
        "signal_run_id": signal_run_id,
        "tier_counts": tier_counts,
        "market_regime": market_regime,
        "tradability_gate_failed_count": tradability_gate_failed_count,
        "symbol_master_count": len(symbol_master_set),
        "symbol_master_snapshot_date": symbol_master_snapshot_date,
        "symbol_master_source": symbol_master_source,
        "prequalified_count": len(prequalified),
        "valid_tickers_count": len(valid_tickers),
        "source_counts": source_counts,
        "source_health": source_health,
        "validation_diagnostics": {
            "mode": validation_mode,
            "provider_unavailable": bool(validation_diag.get("provider_unavailable")),
            "auth_error_count": int(validation_diag.get("auth_error_count") or 0),
            "errors": validation_diag.get("errors") or [],
        },
        "errors": fetch_errors,
        "notes": {
            "pipeline": "extract -> context/ambiguity -> symbol-master -> minimum-evidence -> tiered-candidate-quality -> weighted-cross-source -> validation -> ranking",
            "normalization": "Adds abnormal-buzz metrics using current 1h vs rolling 30d hourly baseline.",
            "window": "Momentum uses mentions in last 6h vs previous 6h.",
            "sentiment": "Keyword-based sentiment classification (bullish/bearish/neutral).",
            "tiering": "Candidate tiers: A (>=2.6), B (>=1.8), C (<1.8) by calibrated confidence score.",
            "classifier": "Mention-quality classifier blends qualified ratio, context density, author diversity, cashtag density, and weighted source confirmation.",
            "ticker_validation": "Only tier A/B candidates are quote-validated.",
            "tradability": "Adds tradability scoring/gating from liquidity, spread, volatility, market-cap normalization, and market regime multiplier.",
            "catalysts": "Adds catalyst tags from mention text (earnings/analyst/corporate/regulatory/macro) and options context intensity.",
            "reddit_ingestion": "Uses low-rate public Reddit listing endpoints with pacing/retry/backoff, then old.reddit HTML fallback.",
            "reddit_fast_fail": "Skips remaining Reddit sources in-run after repeated blocked/timeout responses.",
            "reddit_timeout_sec": f"{max(2, REDDIT_FETCH_TIMEOUT_SEC)}",
            "stocktwits_timeout_sec": f"{max(1, STOCKTWITS_FETCH_TIMEOUT_SEC)}",
            "reddit_circuit_breaker": f"Per-source cooldown after {SOCIAL_SOURCE_FAIL_THRESHOLD} consecutive failures for {SOCIAL_SOURCE_COOLDOWN_SEC}s.",
            "pushshift": "Disabled in production ingestion path.",
            "apewisdom_fallback": "Uses ApeWisdom WallStreetBets aggregated rankings as a non-Reddit fallback source when available.",
            "stocktwits_trending": "Uses Stocktwits trending symbols endpoint as an additional fast discovery source.",
            "skip_reddit_if_fallback_ready": "true" if SOCIAL_SKIP_REDDIT_IF_FALLBACK_READY else "false",
            "tiktok_source": "Optional provider-based source. Enable with TIKTOK_PROVIDER_URL and TIKTOK_PROVIDER_API_KEY. No scraping path is used.",
            "validation_mode": "Uses quote-validation by default; auto-falls back to high-confidence symbol-master set on temporary provider auth outages.",
            "quote_validation_enabled": "true" if SOCIAL_QUOTE_VALIDATION_ENABLED else "false",
            "quote_provider_cooldown_sec": str(quote_provider_cooldown_remaining_sec()),
            "scan_budget": f"Scan budget {max(5.0, SOCIAL_SCAN_TIME_BUDGET_SEC):.1f}s; Stocktwits fan-out capped at {max(1, SOCIAL_MAX_STOCKTWITS_SYMBOLS)} symbols.",
            "scan_budget_hit": "true" if scan_budget_hit else "false",
            "model_id": SCORING_MODEL_ID,
            "caveat": "Social sentiment is noisy and should be combined with price/volume confirmation.",
        },
    }
    set_cached(cache_key, payload)
    return payload


# ---------------------------------------------------------------------------
# Stale-while-revalidate serving for the social scanner.
#
# The live scan above takes 30-60s (paced Reddit + Stocktwits + ApeWisdom),
# which is longer than any sane browser/proxy timeout — serving it inline is
# why the Social tab used to time out. Instead the endpoint always answers
# instantly from the last completed scan and refreshes in the background:
#   - result older than SOCIAL_RESULT_REFRESH_AFTER_SEC (default 10 min) or
#     ?refresh=true  ->  single-flight background rescan
#   - process restart  ->  last persisted signal run is restored from SQLite
# ---------------------------------------------------------------------------
SOCIAL_RESULT_REFRESH_AFTER_SEC = int(os.getenv("SOCIAL_RESULT_REFRESH_AFTER_SEC", "600"))
_social_result_lock = threading.Lock()
_social_result_state: dict[str, Any] = {
    "payload": None,
    "completed_at": 0.0,
    "running": False,
    "started_at": 0.0,
    "last_error": None,
}


def _social_seed_from_db() -> None:
    """Restore the last persisted signal run so restarts don't serve an empty tab."""
    try:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                run = conn.execute(
                    "SELECT run_id, created_at FROM social_signal_run ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if not run:
                    return
                rows = conn.execute(
                    "SELECT payload_json FROM social_signal_row WHERE run_id = ? ORDER BY rank_position ASC",
                    (str(run[0]),),
                ).fetchall()
            finally:
                conn.close()
        parsed: list[dict[str, Any]] = []
        for (payload_json,) in rows:
            try:
                parsed.append(json.loads(payload_json))
            except Exception:
                continue
        if not parsed:
            return
        created_at = str(run[1] or "")
        try:
            completed_ts = datetime.fromisoformat(created_at).timestamp()
        except Exception:
            completed_ts = 0.0
        payload = {
            "updated_at": created_at,
            "count": len(parsed),
            "rows": parsed,
            "source_counts": {},
            "source_health": {},
            "errors": {},
            "notes": {"restored": "Restored from the last persisted scan after a restart; background refresh will update it."},
        }
        with _social_result_lock:
            if _social_result_state["payload"] is None:
                _social_result_state["payload"] = payload
                _social_result_state["completed_at"] = completed_ts
        logger.info("Social sentiment seeded from persisted run %s (%d rows)", run[0], len(parsed))
    except Exception as exc:
        logger.warning("Social sentiment seed-from-db failed: %s", exc)


def _social_refresh_worker(experiment_id: str | None) -> None:
    try:
        payload = run_social_sentiment_scan(limit=100, experiment_id=experiment_id)
        with _social_result_lock:
            _social_result_state["payload"] = payload
            _social_result_state["completed_at"] = time.time()
            _social_result_state["last_error"] = None
    except Exception as exc:
        logger.warning("Social sentiment background refresh failed: %s", exc)
        with _social_result_lock:
            _social_result_state["last_error"] = str(exc)
    finally:
        with _social_result_lock:
            _social_result_state["running"] = False


def ensure_social_refresh(experiment_id: str | None = None) -> bool:
    """Start a background refresh unless one is already running. Returns True if started."""
    with _social_result_lock:
        if _social_result_state["running"]:
            return False
        _social_result_state["running"] = True
        _social_result_state["started_at"] = time.time()
    thread = threading.Thread(target=_social_refresh_worker, args=(experiment_id,), daemon=True)
    thread.start()
    return True


_social_seed_from_db()


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


def parse_iso_dt(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_json_array(value: str | None) -> list[dict[str, Any]]:
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return []
    except Exception:
        return []


def infer_etf_industry(name: str, category: str) -> str:
    hay = f"{name} {category}".lower()
    tags = [
        ("Semiconductors", ("semiconductor", "chip")),
        ("Technology", ("technology", "tech", "software", "internet", "ai")),
        ("Healthcare", ("health", "biotech", "medical", "pharma")),
        ("Financials", ("financial", "bank", "insurance", "fintech")),
        ("Energy", ("energy", "oil", "gas", "uranium", "nuclear")),
        ("Industrials", ("industrial", "aerospace", "defense", "transport")),
        ("Utilities", ("utilities", "utility")),
        ("Real Estate", ("real estate", "reit", "property")),
        ("Consumer Discretionary", ("consumer discretionary", "retail", "travel")),
        ("Consumer Staples", ("consumer staples", "staples")),
        ("Communication Services", ("communication", "media", "telecom")),
        ("Materials", ("materials", "metals", "mining", "steel")),
        ("Fixed Income", ("bond", "treasury", "credit", "fixed income", "municipal")),
        ("Commodities", ("commodity", "gold", "silver", "oil", "agriculture")),
        ("International", ("international", "emerging", "europe", "japan", "china", "global ex")),
        ("Broad Market", ("broad", "s&p", "total market", "all market", "index")),
    ]
    for label, keys in tags:
        if any(k in hay for k in keys):
            return label
    return "Multi-Sector"


ETF_TOP_HOLDINGS_SEED: dict[str, list[dict[str, Any]]] = {
    "SPY": [{"symbol": "AAPL", "weight_pct": 7.0}, {"symbol": "MSFT", "weight_pct": 6.0}, {"symbol": "NVDA", "weight_pct": 5.0}],
    "VOO": [{"symbol": "AAPL", "weight_pct": 7.0}, {"symbol": "MSFT", "weight_pct": 6.0}, {"symbol": "NVDA", "weight_pct": 5.0}],
    "IVV": [{"symbol": "AAPL", "weight_pct": 7.0}, {"symbol": "MSFT", "weight_pct": 6.0}, {"symbol": "NVDA", "weight_pct": 5.0}],
    "QQQ": [{"symbol": "AAPL", "weight_pct": 9.0}, {"symbol": "MSFT", "weight_pct": 8.0}, {"symbol": "NVDA", "weight_pct": 7.0}],
    "VTI": [{"symbol": "AAPL", "weight_pct": 6.0}, {"symbol": "MSFT", "weight_pct": 5.0}, {"symbol": "NVDA", "weight_pct": 4.0}],
    "DIA": [{"symbol": "UNH", "weight_pct": 8.0}, {"symbol": "GS", "weight_pct": 7.0}, {"symbol": "MSFT", "weight_pct": 6.0}],
    "IWM": [{"symbol": "SMCI", "weight_pct": 0.5}, {"symbol": "FTAI", "weight_pct": 0.5}, {"symbol": "RDDT", "weight_pct": 0.5}],
    "XLF": [{"symbol": "BRK.B", "weight_pct": 14.0}, {"symbol": "JPM", "weight_pct": 10.0}, {"symbol": "V", "weight_pct": 8.0}],
    "XLK": [{"symbol": "AAPL", "weight_pct": 20.0}, {"symbol": "MSFT", "weight_pct": 19.0}, {"symbol": "NVDA", "weight_pct": 7.0}],
    "XLE": [{"symbol": "XOM", "weight_pct": 22.0}, {"symbol": "CVX", "weight_pct": 17.0}, {"symbol": "COP", "weight_pct": 6.0}],
    "XLV": [{"symbol": "LLY", "weight_pct": 13.0}, {"symbol": "UNH", "weight_pct": 9.0}, {"symbol": "JNJ", "weight_pct": 8.0}],
    "XLI": [{"symbol": "GE", "weight_pct": 4.0}, {"symbol": "CAT", "weight_pct": 4.0}, {"symbol": "RTX", "weight_pct": 4.0}],
    "XLY": [{"symbol": "AMZN", "weight_pct": 23.0}, {"symbol": "TSLA", "weight_pct": 14.0}, {"symbol": "HD", "weight_pct": 6.0}],
    "XLP": [{"symbol": "PG", "weight_pct": 15.0}, {"symbol": "COST", "weight_pct": 12.0}, {"symbol": "WMT", "weight_pct": 9.0}],
    "XLB": [{"symbol": "LIN", "weight_pct": 17.0}, {"symbol": "SHW", "weight_pct": 8.0}, {"symbol": "APD", "weight_pct": 7.0}],
    "XLU": [{"symbol": "NEE", "weight_pct": 12.0}, {"symbol": "SO", "weight_pct": 8.0}, {"symbol": "DUK", "weight_pct": 7.0}],
    "TLT": [{"symbol": "TLT", "weight_pct": 70.0}, {"symbol": "IEF", "weight_pct": 20.0}, {"symbol": "SHY", "weight_pct": 10.0}],
    "HYG": [{"symbol": "HYG", "weight_pct": 60.0}, {"symbol": "JNK", "weight_pct": 25.0}, {"symbol": "LQD", "weight_pct": 15.0}],
    "LQD": [{"symbol": "LQD", "weight_pct": 65.0}, {"symbol": "VCIT", "weight_pct": 20.0}, {"symbol": "VCSH", "weight_pct": 15.0}],
    "GLD": [{"symbol": "GLD", "weight_pct": 60.0}, {"symbol": "IAU", "weight_pct": 30.0}, {"symbol": "SGOL", "weight_pct": 10.0}],
    "SLV": [{"symbol": "SLV", "weight_pct": 60.0}, {"symbol": "SIVR", "weight_pct": 25.0}, {"symbol": "PSLV", "weight_pct": 15.0}],
    "USO": [{"symbol": "USO", "weight_pct": 60.0}, {"symbol": "BNO", "weight_pct": 25.0}, {"symbol": "DBO", "weight_pct": 15.0}],
    "UNG": [{"symbol": "UNG", "weight_pct": 60.0}, {"symbol": "UNL", "weight_pct": 25.0}, {"symbol": "BOIL", "weight_pct": 15.0}],
    "BITO": [{"symbol": "BITO", "weight_pct": 55.0}, {"symbol": "IBIT", "weight_pct": 30.0}, {"symbol": "FBTC", "weight_pct": 15.0}],
}


def infer_etf_issuer(name: str) -> str:
    prefixes = [
        "iShares",
        "Vanguard",
        "SPDR",
        "Invesco",
        "ProShares",
        "Direxion",
        "Schwab",
        "VanEck",
        "Global X",
        "ARK",
        "First Trust",
        "WisdomTree",
    ]
    n = str(name or "").strip()
    for p in prefixes:
        if n.lower().startswith(p.lower()):
            return p
    return ""


def _normalized_desc_weights(n: int, start: float = 14.0, decay: float = 0.88) -> list[float]:
    count = max(1, int(n))
    raw: list[float] = []
    current = float(start)
    for _ in range(count):
        raw.append(max(0.2, current))
        current *= float(decay)
    total = sum(raw) or 1.0
    return [round((x / total) * 100.0, 3) for x in raw]


def _compose_holdings(seed_symbols: list[str], pool: list[str], limit: int = 20) -> list[dict[str, Any]]:
    ordered: list[str] = []
    seen: set[str] = set()
    for sym in [*seed_symbols, *pool]:
        key = str(sym or "").strip()
        if not key or key in seen:
            continue
        ordered.append(key)
        seen.add(key)
        if len(ordered) >= int(limit):
            break
    if not ordered:
        ordered = ["AAPL", "MSFT", "NVDA"]
    weights = _normalized_desc_weights(len(ordered))
    return [{"symbol": sym, "weight_pct": weights[idx]} for idx, sym in enumerate(ordered)]


def synthesize_holdings_for_etf(symbol: str, name: str, industry: str, category: str) -> list[dict[str, Any]]:
    text = f"{symbol} {name} {industry} {category}".lower()
    pools: dict[str, list[str]] = {
        "semiconductors": ["NVDA", "AVGO", "AMD", "QCOM", "TXN", "MU", "INTC", "ADI", "NXPI", "MCHP", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "ASML", "TSM", "ARM", "GFS", "MPWR"],
        "technology": ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AVGO", "ORCL", "CRM", "ADBE", "CSCO", "INTC", "IBM", "QCOM", "AMD", "NOW", "PANW", "SNOW", "PLTR"],
        "financials": ["BRK.B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "BLK", "SCHW", "C", "SPGI", "CB", "PGR", "MMC", "KKR", "BX", "AON", "ICE"],
        "energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "KMI", "WMB", "HAL", "BKR", "DVN", "FANG", "HES", "CTRA", "PXD", "TRGP", "EPD"],
        "healthcare": ["LLY", "UNH", "JNJ", "MRK", "ABBV", "TMO", "ABT", "DHR", "PFE", "BMY", "ISRG", "AMGN", "MDT", "VRTX", "GILD", "REGN", "BSX", "CVS", "CI", "SYK"],
        "industrials": ["GE", "CAT", "RTX", "BA", "LMT", "NOC", "GD", "DE", "HON", "ETN", "EMR", "PH", "ITW", "CMI", "TT", "CSX", "UNP", "NSC", "UPS", "FDX"],
        "consumer_discretionary": ["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "BKNG", "TJX", "LOW", "CMG", "RCL", "MAR", "ORLY", "AZO", "YUM", "ROST", "EBAY", "LEN", "DHI", "GM"],
        "consumer_staples": ["PG", "COST", "WMT", "KO", "PEP", "PM", "MO", "MDLZ", "CL", "KMB", "MNST", "KHC", "GIS", "KR", "STZ", "ADM", "EL", "HSY", "SYY", "TSN"],
        "communication": ["GOOGL", "META", "NFLX", "DIS", "TMUS", "T", "VZ", "CMCSA", "CHTR", "WBD", "PARA", "EA", "TTWO", "ROKU", "SNAP", "PINS", "SPOT", "LYV", "FOXA", "FOX"],
        "materials": ["LIN", "SHW", "APD", "ECL", "NEM", "FCX", "NUE", "DD", "DOW", "PPG", "MLM", "VMC", "STLD", "ALB", "IFF", "IP", "PKG", "BALL", "CE", "CF"],
        "utilities": ["NEE", "SO", "DUK", "AEP", "EXC", "XEL", "SRE", "ED", "PEG", "WEC", "D", "EIX", "AWK", "PPL", "ETR", "AES", "CMS", "LNT", "NI", "FE"],
        "real_estate": ["PLD", "AMT", "EQIX", "WELL", "SPG", "O", "DLR", "PSA", "CCI", "CBRE", "VICI", "AVB", "EQR", "EXR", "IRM", "SBAC", "INVH", "ARE", "MAA", "BXP"],
        "fixed_income": ["TLT", "IEF", "SHY", "BND", "AGG", "HYG", "LQD", "JNK", "EMB", "TIP", "SGOV", "BIL", "VCIT", "VCSH", "IGSB", "MBB", "MUB", "GOVT", "FLOT", "SPHY"],
        "commodities": ["GLD", "IAU", "SLV", "USO", "UNG", "DBC", "PDBC", "GSG", "BCOM", "COMT", "CPER", "DBA", "WEAT", "CORN", "SOYB", "JO", "UGA", "BNO", "PALL", "PLTM"],
        "broad_market": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA", "BRK.B", "LLY", "JPM", "UNH", "XOM", "V", "MA", "COST", "WMT", "HD", "PG", "JNJ"],
        "small_mid": ["SMCI", "RDDT", "FTAI", "APLD", "CELH", "NU", "SOFI", "RKLB", "HIMS", "PLTR", "IOT", "DUOL", "SNOW", "DDOG", "NET", "CRWD", "MDB", "U", "AFRM", "UPST"],
        "crypto": ["BITO", "IBIT", "FBTC", "ARKB", "ETHA", "ETHE", "BITQ", "BLOK", "WGMI", "COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT", "IREN", "CIFR", "BTBT", "NVDA", "SMCI"],
    }

    if any(k in text for k in ("semiconductor", "chip", "soxx")):
        key = "semiconductors"
    elif any(k in text for k in ("financial", "bank", "xlf")):
        key = "financials"
    elif any(k in text for k in ("energy", "oil", "gas", "xle")):
        key = "energy"
    elif any(k in text for k in ("health", "biotech", "pharma", "xlv")):
        key = "healthcare"
    elif any(k in text for k in ("industrial", "aerospace", "defense", "xli")):
        key = "industrials"
    elif any(k in text for k in ("consumer discretionary", "retail", "xly")):
        key = "consumer_discretionary"
    elif any(k in text for k in ("consumer staples", "xlp")):
        key = "consumer_staples"
    elif any(k in text for k in ("communication", "media", "telecom")):
        key = "communication"
    elif any(k in text for k in ("materials", "mining", "metals", "xlb")):
        key = "materials"
    elif any(k in text for k in ("utility", "xlu")):
        key = "utilities"
    elif any(k in text for k in ("real estate", "reit")):
        key = "real_estate"
    elif any(k in text for k in ("bond", "treasury", "fixed income", "credit", "municipal", "tlt", "hyg", "lqd")):
        key = "fixed_income"
    elif any(k in text for k in ("commodity", "gold", "silver", "futures", "uso", "ung", "gld", "slv")):
        key = "commodities"
    elif any(k in text for k in ("bitcoin", "crypto", "bito")):
        key = "crypto"
    elif any(k in text for k in ("small cap", "mid cap", "russell", "iwm")):
        key = "small_mid"
    elif any(k in text for k in ("broad market", "s&p", "total market", "index", "spy", "voo", "ivv", "vti", "qqq")):
        key = "broad_market"
    elif str(industry or "").lower() == "semiconductors":
        key = "semiconductors"
    elif str(industry or "").lower() == "financials":
        key = "financials"
    elif str(industry or "").lower() == "energy":
        key = "energy"
    elif str(industry or "").lower() == "healthcare":
        key = "healthcare"
    elif str(industry or "").lower() == "fixed income":
        key = "fixed_income"
    elif str(industry or "").lower() == "commodities":
        key = "commodities"
    elif str(industry or "").lower() == "real estate":
        key = "real_estate"
    elif str(industry or "").lower() == "utilities":
        key = "utilities"
    else:
        key = "technology"

    seed = ETF_TOP_HOLDINGS_SEED.get(str(symbol or "").upper(), [])
    seed_symbols = [str(h.get("symbol") or "").strip() for h in seed if str(h.get("symbol") or "").strip()]
    pool = pools.get(key, pools["technology"])
    return _compose_holdings(seed_symbols, pool, limit=20)


def fetch_research_top_etfs(limit: int) -> list[dict[str, Any]]:
    payload = {
        "range": [0, max(50, min(1200, int(limit * 2)))],
        "sort": {"sortBy": "volume", "sortOrder": "desc"},
        "filter": [{"left": "type", "operation": "equal", "right": "fund"}],
        "columns": ["name", "description", "exchange", "close", "change", "volume", "average_volume_30d_calc"],
    }
    raw = fetch_json_url(
        ETF_RESEARCH_SCAN_URL,
        timeout=max(6, MARKET_DATA_TIMEOUT_SEC),
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    items = raw.get("data") if isinstance(raw, dict) else []
    rows: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        full_symbol = str(item.get("s") or "").strip()
        if not full_symbol:
            continue
        symbol = full_symbol.split(":")[-1].upper().strip()
        fields = item.get("d") if isinstance(item.get("d"), list) else []
        if len(fields) < 7:
            continue
        name = str(fields[1] or fields[0] or symbol).strip()
        category = name
        industry = infer_etf_industry(name, category)
        synthesized = synthesize_holdings_for_etf(symbol, name, industry, category)
        seed_holdings = ETF_TOP_HOLDINGS_SEED.get(symbol, [])
        if seed_holdings:
            seed_symbols = [str(h.get("symbol") or "").strip() for h in seed_holdings if str(h.get("symbol") or "").strip()]
            pool_symbols = [str(h.get("symbol") or "").strip() for h in synthesized if str(h.get("symbol") or "").strip()]
            holdings = _compose_holdings(seed_symbols, pool_symbols, limit=20)
        else:
            holdings = synthesized
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "exchange": str(fields[2] or ""),
                "price": finite_or(fields[3], 0),
                "change_pct": to_float(fields[4]),
                "volume": finite_or(fields[5], 0),
                "avg_volume": to_float(fields[6]),
                "industry": industry,
                "category": category,
                "issuer": infer_etf_issuer(name),
                "expense_ratio": None,
                "total_assets": None,
                "largest_holdings": holdings,
                "largest_holdings_text": format_holdings_brief(holdings),
                "source": "research_seed",
            }
        )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(row)
        if len(out) >= limit:
            break
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out


def format_holdings_brief(holdings: list[dict[str, Any]]) -> str:
    items: list[str] = []
    for item in holdings[:3]:
        sym = str(item.get("symbol") or "").strip()
        name = str(item.get("name") or "").strip()
        label = sym or name
        if not label:
            continue
        pct = to_float(item.get("weight_pct"))
        if pct is None:
            items.append(label)
        else:
            items.append(f"{label} ({pct:.2f}%)")
    return ", ".join(items)


def _load_popular_etf_snapshot_rows(conn: sqlite3.Connection, snapshot_date: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT popularity_rank, symbol, name, exchange, price, change_pct, volume, avg_volume, industry, category, issuer,
               expense_ratio, total_assets, largest_holdings_json, source, updated_at
        FROM popular_etf_snapshot
        WHERE snapshot_date = ?
        ORDER BY popularity_rank ASC
        LIMIT ?
        """,
        (snapshot_date, int(limit)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        symbol = str(r[1] or "")
        name = str(r[2] or "")
        industry = str(r[8] or "")
        category = str(r[9] or "")
        holdings = parse_json_array(str(r[13] or "[]"))
        if len(holdings) < 20:
            seed = ETF_TOP_HOLDINGS_SEED.get(symbol, [])
            seed_symbols = [str(h.get("symbol") or "").strip() for h in seed if str(h.get("symbol") or "").strip()]
            synthetic = synthesize_holdings_for_etf(symbol, name, industry, category)
            synthetic_symbols = [str(h.get("symbol") or "").strip() for h in synthetic if str(h.get("symbol") or "").strip()]
            merged = _compose_holdings(seed_symbols, synthetic_symbols, limit=20)
            holdings = merged
        out.append(
            {
                "rank": int(r[0] or 0),
                "symbol": symbol,
                "name": name,
                "exchange": str(r[3] or ""),
                "price": finite_or(r[4], 0),
                "change_pct": to_float(r[5]),
                "volume": finite_or(r[6], 0),
                "avg_volume": to_float(r[7]),
                "industry": industry,
                "category": category,
                "issuer": str(r[10] or ""),
                "expense_ratio": to_float(r[11]),
                "total_assets": to_float(r[12]),
                "largest_holdings": holdings,
                "largest_holdings_text": format_holdings_brief(holdings),
                "source": str(r[14] or ""),
                "updated_at": str(r[15] or ""),
            }
        )
    return out


def _persist_popular_etf_snapshot(conn: sqlite3.Connection, snapshot_date: str, rows: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM popular_etf_snapshot WHERE snapshot_date = ?", (snapshot_date,))
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO popular_etf_snapshot
            (snapshot_date, popularity_rank, symbol, name, exchange, price, change_pct, volume, avg_volume, industry, category,
             issuer, expense_ratio, total_assets, largest_holdings_json, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_date,
                int(row.get("rank") or 0),
                str(row.get("symbol") or ""),
                str(row.get("name") or ""),
                str(row.get("exchange") or ""),
                finite_or(row.get("price"), 0),
                to_float(row.get("change_pct")),
                finite_or(row.get("volume"), 0),
                to_float(row.get("avg_volume")),
                str(row.get("industry") or ""),
                str(row.get("category") or ""),
                str(row.get("issuer") or ""),
                to_float(row.get("expense_ratio")),
                to_float(row.get("total_assets")),
                json.dumps(row.get("largest_holdings") or []),
                str(row.get("source") or ""),
                now_iso(),
            ),
        )
    # Keep 180 days of snapshots.
    cutoff = (datetime.now(UTC) - timedelta(days=180)).date().isoformat()
    conn.execute("DELETE FROM popular_etf_snapshot WHERE snapshot_date < ?", (cutoff,))


def get_popular_etf_snapshot(limit: int = ETF_POPULAR_LIMIT, refresh: bool = False) -> dict[str, Any]:
    cap = max(1, min(500, int(limit)))
    cache_key = f"etf:popular:{cap}"
    if not refresh:
        cached = get_cached(cache_key, 120)
        if cached is not None:
            return cached

    today = datetime.now(UTC).date().isoformat()
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            if not refresh:
                existing_rows = _load_popular_etf_snapshot_rows(conn, today, cap)
                if len(existing_rows) >= cap:
                    payload = {
                        "updated_at": now_iso(),
                        "snapshot_date": today,
                        "count": len(existing_rows),
                        "rows": existing_rows,
                        "source": "sqlite_snapshot",
                    }
                    set_cached(cache_key, payload)
                    return payload
        finally:
            conn.close()

    seeded_rows = fetch_research_top_etfs(cap)
    if not seeded_rows:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                _, historical_rows = "", []
                snap = conn.execute("SELECT snapshot_date FROM popular_etf_snapshot ORDER BY snapshot_date DESC LIMIT 1").fetchone()
                if snap and snap[0]:
                    historical_rows = _load_popular_etf_snapshot_rows(conn, str(snap[0]), cap)
            finally:
                conn.close()
        payload = {
            "updated_at": now_iso(),
            "snapshot_date": today,
            "count": len(historical_rows),
            "rows": historical_rows,
            "source": "sqlite_fallback",
        }
        set_cached(cache_key, payload)
        return payload

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            _persist_popular_etf_snapshot(conn, today, seeded_rows)
            conn.commit()
            stored_rows = _load_popular_etf_snapshot_rows(conn, today, cap)
        finally:
            conn.close()

    payload = {
        "updated_at": now_iso(),
        "snapshot_date": today,
        "count": len(stored_rows),
        "rows": stored_rows,
        "source": "research_seed",
    }
    set_cached(cache_key, payload)
    return payload


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
