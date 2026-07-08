"""SQLite persistence: the shared social_baseline.db connection factory
(schema DDL runs once per process) and its lock. Backs social baselines,
symbol-master snapshots, SWR cache, and after-hours snapshots."""

from __future__ import annotations

import sqlite3
import threading

from .config import SOCIAL_BASELINE_DB

_social_db_lock = threading.Lock()
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
