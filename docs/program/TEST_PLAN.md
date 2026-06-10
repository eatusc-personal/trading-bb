# Test Plan

## Test Matrix by Phase

### Phase 1
- Unit: rate-limit/backoff behavior, source error handling.
- Integration: Reddit public listing ingestion success/failure paths.
- Acceptance: scanner remains available during single-source outage.

### Phase 2
- Unit: symbol snapshot merge/diff logic, alias mapping.
- Integration: symbol lookup at historical date, cache fallback behavior.
- Integration: `/api/social/symbol-master/resolve` deterministic output for fixed `as_of` date.
- Integration: separate symbol-validity cache and quote-liveness cache behavior under provider errors.
- Acceptance: deterministic symbol validity resolution by snapshot date.
- Executed checks:
  - `/api/social/symbol-master/health`
  - `/api/social/symbol-master/snapshots`
  - `/api/social/symbol-master/alias/health`
  - `POST /api/social/symbol-master/alias/refresh`
  - `npm run metrics:phase2-repro`

### Phase 3
- Unit: candidate tiering, ambiguity handling, classifier scoring.
- Integration: end-to-end ranked output with explainability fields.
- Acceptance: measurable false-positive reduction vs baseline sample.
- Executed checks:
  - `GET /api/social/sentiment?limit=20` includes:
    - `model_id`
    - `tier_counts`
    - row fields `candidate_tier`, `mention_quality_score`, `weighted_source_score`, `explainability`
  - `npm run metrics:phase3-eval` runs holdout baseline-vs-current comparison from `docs/program/PHASE3_BENCHMARK.json`.

### Phase 4
- Unit: liquidity and spread gating rules.
- Integration: ranking payload includes tradability/catalyst context.
- Acceptance: risk-adjusted metric uplift vs buzz-only rank baseline.
- Executed checks:
  - `GET /api/social/sentiment?limit=20` row payload includes:
    - `tradability_score`, `tradability_gate_passed`, `tradability_gate_reasons`
    - `spread_bps`, `annualized_volatility_pct`, `market_cap_bucket`, `market_regime`
    - `catalyst_tags`, `options_context_score`

### Phase 5
- Unit: event store and signal store write integrity.
- Integration: backtest runner reproducibility by experiment ID.
- Acceptance: standardized reports generated for each experiment run.
- Executed checks:
  - `/api/social/sentiment` now returns `signal_run_id` and accepts optional `experiment_id`.
  - `GET /api/social/signal-lab/runs` lists persisted signal runs.
  - `GET /api/social/signal-lab/run/{run_id}` returns ranked row payload snapshots.
  - `GET /api/social/signal-lab/event-study/{run_id}` returns horizon-based hit rate/return/alpha summary.
  - `npm run metrics:phase5-event-study` evaluates the latest persisted run (or explicit run ID via script args).

### Phase 6
- Unit: auth/rate-limit middleware behavior.
- Integration: load tests and failure-injection scenarios.
- Acceptance: operational runbook validated in tabletop test.
- Executed checks:
  - `GET /api/security/status` returns active CORS origins, auth-enabled flag, and rate-limit configuration.
  - `/api/social/*` endpoints enforce `SOCIAL_API_KEY` when configured.
  - Rate-limit middleware returns `429` with `Retry-After` on sustained bursts.
  - `python3 scripts/phase6_security_check.py ... --api-key ...` returns `ok=true` for combined health/auth/proxy/rate-limit checks.
  - Incident-response tabletop scenarios validated against runbook:
    - auth misconfiguration diagnosis/recovery
    - CORS allowlist mismatch diagnosis/recovery
    - local rate-limit threshold tuning workflow

## Global Regression Suite
- Existing API contracts remain backward compatible unless explicitly versioned.
- Dashboard pages continue loading with partial backend degradation.
- Social sentiment endpoint returns diagnostics without exposing sensitive data.
