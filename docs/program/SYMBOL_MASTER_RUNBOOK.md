# Symbol Master Runbook

## Purpose
Operational checks and fallback behavior for Phase 2 symbol-master snapshot and resolution flow.

## Endpoints
- Health: `/api/social/symbol-master/health`
- Snapshot list: `/api/social/symbol-master/snapshots?limit=60`
- Resolve by date: `/api/social/symbol-master/resolve?symbol=AAPL&as_of=2026-03-18`
- Alias health: `/api/social/symbol-master/alias/health`
- Alias refresh: `POST /api/social/symbol-master/alias/refresh`
- Social sentiment (includes snapshot metadata): `/api/social/sentiment?limit=5`

## Quick Checks
Run from project root:

```bash
curl -sS 'http://127.0.0.1:3000/api/social/symbol-master/health'
curl -sS 'http://127.0.0.1:3000/api/social/symbol-master/snapshots?limit=5'
curl -sS 'http://127.0.0.1:3000/api/social/symbol-master/resolve?symbol=AAPL&as_of=2026-03-18'
curl -sS -X POST 'http://127.0.0.1:3000/api/social/symbol-master/alias/refresh'
curl -sS 'http://127.0.0.1:3000/api/social/symbol-master/alias/health'
curl -sS 'http://127.0.0.1:3000/api/social/sentiment?limit=5'
npm run metrics:phase2-repro
```

Expected:
- health returns `current_snapshot_date`, `snapshot_count`, `current_snapshot_symbol_count`.
- snapshots returns date/row-count history used for as-of resolution.
- resolve returns deterministic `resolution` for `as_of_date`.
- alias health returns `alias_count`, `last_refreshed_on`, and `sample`.
- sentiment includes `symbol_master_snapshot_date` and `symbol_master_source`.

## Snapshot Fallback Order
1. Load same-day snapshot from SQLite if present and sufficiently populated.
2. Attempt remote refresh from Nasdaq symbol files and persist new snapshot.
3. If remote fails, use latest persisted snapshot.
4. Last-resort use partial same-day rows.

## Cache Layers
- Symbol validity cache (persistent, SQLite):
  - Table: `symbol_validity_cache`
  - Key: `(symbol, as_of_date)`
  - Stores `is_valid`, `resolved_symbol`, snapshot provenance and check time.
- Quote liveness cache (short-lived):
  - In-memory key: `social:quote_live:{symbol}:{provider}` (~60s).
  - Persistent minute buckets in SQLite table: `quote_liveness_cache`.

## Alias Behavior
- Alias mapping is read from SQLite table `symbol_alias`.
- Resolver applies aliases by `as_of_date` window (`effective_from` / `effective_to`).
- If no alias matches, resolver falls back to the input symbol.
- Seed file path defaults to `python_api/symbol_alias_seed.json` and can be overridden by `SYMBOL_ALIAS_SEED_PATH`.

## Maintenance
- Snapshot retention: ~540 days.
- Validity cache retention: ~180 days.
- Quote liveness retention: ~7 days (minute buckets).
