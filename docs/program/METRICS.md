# Metrics and Experiment Tracking

## Baseline Definitions
- Baseline date: 2026-03-18
- Baseline model ID: `social_v1`
- Baseline data window: last 30 days where available

## Core Product Metrics
- False-positive ticker rate
- Precision@k (k = 5, 10, 20)
- Hit rate by horizon (30m, 2h, 1d, 3d, 5d)
- Risk-adjusted returns (Sharpe, Sortino)
- Max drawdown in strategy simulation
- API latency p50/p95 (`/api/social/sentiment`)
- Source fetch success rate by provider

## Experiment Log Template
- Experiment ID:
- Date range:
- Model/rule version:
- Feature set:
- Universe constraints:
- Cost/slippage assumptions:
- Results summary:
- Decision:

## Metric Targets (Program-Level)
- False-positive rate: >= 40% reduction from baseline.
- Precision@10: >= 25% improvement vs baseline.
- Warm-cache p95 latency: under agreed threshold for local workload.
- Source failure resilience: endpoint returns partial results rather than hard fail in expected outage scenarios.

## Reporting Cadence
- Daily: latency + source health + blocker notes.
- Weekly: precision/hit-rate trend + major rule changes.
- Milestone: full backtest comparison and acceptance summary.

## Baseline Snapshot (2026-03-18 Runtime Smoke)
- Environment: local dev servers (`npm run dev`), public Reddit ingestion mode.
- `/api/market/overview`: returned populated gainers/losers/active payload.
- `/api/news/headlines?limit=10`: returned 10 rows.
- `/api/social/sentiment?limit=5`: returned 5 ranked rows.
- Response diagnostics confirmed:
  - `source_health` object present.
  - `source_health` now includes `retry_count`, `skipped_count`, `cooldown_remaining_sec`, `total_elapsed_ms`, `max_elapsed_ms`, `last_origin`.
  - Notes include `reddit_ingestion` and `pushshift` disabled message.
- UI HTML token checks:
  - `/technical` includes `Technical View`, `Recently Searched`, `MACD`.
  - `/` includes `Market Overview`, `After Hours Movers`, `Alerts / Flags`, `Industries`.

## Latency Samples (`/api/social/sentiment`)
Use:

```bash
npm run metrics:social-latency
```

Sample log format:
- `YYYY-MM-DD HH:MM:SSZ`: samples=N, failures=X, p50=...ms, p95=...ms, mode=..., provider_unavailable=...

### Recorded Samples
- 2026-03-18 18:23:09Z: samples=5, failures=0, p50=9.3ms, p95=21163.2ms, mode=quote_validation, provider_unavailable=false.

### Symbol Master Snapshot Checks
- 2026-03-18: `/api/social/sentiment?limit=5` returned:
  - `symbol_master_snapshot_date="2026-03-18"`
  - `symbol_master_source="remote_refresh"` and later `symbol_master_source="sqlite_snapshot"` on subsequent calls.
- 2026-03-18 (Phase 2):
  - `/api/social/symbol-master/health` returned:
    - `current_snapshot_date="2026-03-18"`
    - `current_snapshot_symbol_count=10945`
    - `snapshot_count=1`
  - `/api/social/symbol-master/resolve?symbol=AAPL&as_of=2026-03-18` returned:
    - `resolution.is_valid=true`
    - deterministic `snapshot_date="2026-03-18"`
    - cache progression from `computed` to `sqlite_validity` on repeated call.
  - `POST /api/social/symbol-master/alias/refresh` returned:
    - `alias_count=11`
    - `refreshed_on="2026-03-18"`
  - `/api/social/symbol-master/resolve` alias checks:
    - `FB` resolved to `META` (`alias_applied=true`)
    - `SQ` resolved to `XYZ` (`alias_applied=true`)
    - `BRK-B` resolved to `BRK.B` (`alias_applied=true`)
  - `/api/social/symbol-master/snapshots?limit=5` returned:
    - one snapshot row for `2026-03-18` with `rows=10945`.
  - `npm run metrics:phase2-repro` returned:
    - `deterministic=true`
    - symbols checked: `AAPL, FB, SQ, BRK-B`
    - runs per symbol: `3`
    - `alias_count=20`

### Optional Source Hooks
- 2026-03-18: social notes include `tiktok_source` indicating provider-based no-scrape integration path is available when configured.

## Phase 3 Evaluation Harness
- Runner:
  - `npm run metrics:phase3-eval`
  - script: `scripts/phase3_eval.py`
- Benchmark file:
  - `docs/program/PHASE3_BENCHMARK.json`
- Current model ID:
  - `social_v4_tradability_context`
- Reported metrics:
  - Precision@5/10/20
  - False-positive rate @5/10/20
  - Percentage uplift/reduction vs `baseline_model_id` from benchmark file.
- Notes:
  - The bundled benchmark file is a seed set for harness verification and must be replaced with the finalized holdout labeling set before Phase 3 acceptance sign-off.

## Phase 4 Metrics Additions
- Tradability diagnostics now emitted per ranked row:
  - `tradability_score`
  - `tradability_gate_passed`
  - `spread_bps`
  - `annualized_volatility_pct`
  - `market_cap_bucket`
  - `market_regime`
- Catalyst/options overlays now emitted per ranked row:
  - `catalyst_tags`
  - `options_context_score`

## Phase 5 Tracking Additions
- Social scans now emit:
  - `signal_run_id`
  - optional `experiment_id` echo
- Run retrieval APIs:
  - `GET /api/social/signal-lab/runs`
  - `GET /api/social/signal-lab/run/{run_id}`
- Event-study API:
  - `GET /api/social/signal-lab/event-study/{run_id}`
- Runner:
  - `npm run metrics:phase5-event-study`
  - script: `scripts/phase5_event_study.py`

## Phase 6 Hardening Metrics
- Security status endpoint:
  - `GET /api/security/status`
- Configuration coverage:
  - CORS origin allowlist (`CORS_ALLOW_ORIGINS`)
  - social auth enabled flag (`SOCIAL_API_KEY`)
  - API/social rate-limit windows and thresholds

### Runtime Verification (2026-03-19)
- `GET /api/security/status` returned:
  - `cors_allow_origins=["http://localhost:3000","http://127.0.0.1:3000"]`
  - `social_api_key_configured=false` (default run)
  - `social_api_key_configured=true` (run with `SOCIAL_API_KEY=testkey123`)
- Burst test (`/api/social/sentiment?limit=5`, 135 calls):
  - `121 x 200`, `14 x 429`
  - `retry-after` header observed on `429` via Next proxy.
- Auth enforcement test with `SOCIAL_API_KEY=testkey123`:
  - direct Python social call without key: `401`
  - direct Python social call with `x-api-key`: `200`
  - Next proxied social call: `200` (server-side key forwarding active)
- Automated check:
  - `python3 scripts/phase6_security_check.py --web-base http://127.0.0.1:3000 --api-base http://127.0.0.1:8001 --api-key testkey123 --burst 135 --timeout 120`
  - Result: `ok=true`, code counts `{"200":117,"429":18}`, `retry_after_seen=true`.
- Incident-response tabletop evidence:
  - Auth misconfig scenario: pass (`401` without key / `200` with key).
  - CORS mismatch scenario: pass (allowlist visible in `/api/security/status` and runbook remediation confirmed).
  - Rate-limit false-positive scenario: pass (`429` + `Retry-After` observed and threshold tuning workflow validated).
