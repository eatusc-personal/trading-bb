# Program Status

> Solo project. The "phase" model is a self-imposed structure to stage the
> build, not a team program. Dates are approximate build milestones. "Complete"
> means feature-complete for that phase's scope; Phase 3's benchmark still uses
> a seed labeling set (see METRICS.md) rather than a finalized holdout.

## Snapshot
- Current phase: Phase 6
- Overall status: Feature-complete (solo build)

## Phase Board
- Phase 1: Complete
- Phase 2: Complete
- Phase 3: Complete
- Phase 4: Complete
- Phase 5: Complete
- Phase 6: Complete

## Current Sprint Focus
- Phase 6 closeout complete; monitor/tune thresholds in operations as needed.

## Blockers
- None currently logged.

## Daily Log
### 2026-03-18
- Completed:
  - Program documentation scaffold created under `docs/program`.
  - Phase sequencing, gates, and metrics definitions drafted.
  - Implemented public Reddit ingestion with compliant Reddit user-agent path.
  - Added Reddit request pacing (`REDDIT_MIN_INTERVAL_SEC`) and low-rate public fallback limit.
  - Added retry/backoff controls for public Reddit listing fetches.
  - Removed Pushshift from `/api/social/sentiment` execution path.
  - Added `source_health` diagnostics to social sentiment response payload.
  - Added per-source retry/timing telemetry fields (`retry_count`, `total_elapsed_ms`, `max_elapsed_ms`, `last_origin`).
  - Added per-source circuit-breaker cooldown support with skip telemetry (`skipped_count`, `cooldown_remaining_sec`).
  - Added Social page Source Health panel for retries/failures/skips/cooldown and timing diagnostics.
  - Added symbol-master SQLite snapshot persistence with snapshot metadata in social API response.
  - Added historical symbol resolver endpoint: `/api/social/symbol-master/resolve?symbol=...&as_of=YYYY-MM-DD`.
  - Added persistent symbol validity cache (`symbol_validity_cache`) keyed by `(symbol, as_of_date)` with snapshot provenance.
  - Split quote liveness from symbol validity with minute-bucket liveness persistence (`quote_liveness_cache`) plus short in-memory TTL.
  - Added alias-ingestion bootstrap from seed file (`python_api/symbol_alias_seed.json`) with daily refresh policy and manual refresh endpoint.
  - Added alias diagnostics endpoints: `/api/social/symbol-master/alias/health` and `POST /api/social/symbol-master/alias/refresh`.
  - Expanded alias seed coverage to 20 mappings (renames + notation aliases).
  - Added snapshot diagnostics endpoint: `/api/social/symbol-master/snapshots`.
  - Added reproducibility checker script + npm task:
    - `scripts/phase2_repro_check.py`
    - `npm run metrics:phase2-repro`
  - Executed reproducibility run: deterministic resolver output across repeated checks (`AAPL, FB, SQ, BRK-B`).
  - Added Phase 2 runbook `docs/program/SYMBOL_MASTER_RUNBOOK.md` with checks and fallback policy.
  - Added optional no-scrape TikTok provider connector hook (API-key based, disabled unless configured).
  - Added README runtime/config docs for public Reddit ingestion and smoke-test curl runbook.
  - Executed runtime smoke tests on market/news/social APIs and key page HTML markers.
  - Removed OAuth implementation from code path to keep deployment no-auth/public-only.
  - Implemented tiered candidate generation rules with explicit confidence bands (`A >= 2.6`, `B >= 1.8`, else `C`).
  - Added calibrated mention-quality classifier output (`mention_quality_score`) to social ranking rows.
  - Added weighted cross-source confirmation scoring (`weighted_source_score`) and source-family mention weighting.
  - Added row-level explainability payload (`top_positive_factors`, `penalties`, `source_mentions`) and model metadata (`model_id`).
  - Added phase-3 evaluation harness script + npm task:
    - `scripts/phase3_eval.py`
    - `npm run metrics:phase3-eval`
  - Added benchmark seed file for holdout evaluation bootstrap:
    - `docs/program/PHASE3_BENCHMARK.json`
  - Started Phase 4 implementation:
    - Added tradability profile scoring/gating (liquidity, spread, volatility, market-cap normalization, market regime).
    - Added catalyst tags from mention text (`earnings`, `analyst`, `corporate`, `regulatory`, `macro`).
    - Added options-context intensity scoring in ranking features.
    - Added tradability diagnostics to row payload (`tradability_score`, gate reasons, spread/volatility, market-cap bucket, regime).
    - Updated Social page table with tradability and catalyst columns.
  - Started Phase 5 implementation:
    - Added signal-lab persistence tables (`social_signal_run`, `social_signal_row`) in SQLite.
    - Added automatic per-scan run persistence with `signal_run_id` and optional `experiment_id`.
    - Added retrieval APIs for persisted runs:
      - `GET /api/social/signal-lab/runs`
      - `GET /api/social/signal-lab/run/{run_id}`
    - Added Next proxy routes for signal-lab APIs.
    - Added event-study API over persisted runs:
      - `GET /api/social/signal-lab/event-study/{run_id}`
    - Added event-study runner script + npm task:
      - `scripts/phase5_event_study.py`
      - `npm run metrics:phase5-event-study`
  - Started Phase 6 implementation:
    - Restricted CORS defaults to explicit local origins (`CORS_ALLOW_ORIGINS`).
    - Added optional social API-key auth guard (`SOCIAL_API_KEY`) for `/api/social/*` endpoints.
    - Added API/social in-memory rate limiting with `429` + `Retry-After`.
    - Added security diagnostics endpoint: `GET /api/security/status`.
    - Added Next proxy route for security status and automatic `x-api-key` forwarding when configured.
    - Added Phase 6 security hardening runbook: `docs/program/SECURITY_RUNBOOK.md`.
    - Added automated Phase 6 checker script + npm task:
      - `scripts/phase6_security_check.py`
      - `npm run metrics:phase6-security`
  - Executed Phase 6 runtime checks:
      - `/api/security/status` reflected configured CORS/rate-limit values and auth-enabled status.
      - Burst social calls produced `429` responses with `Retry-After` propagated through Next proxy.
      - With `SOCIAL_API_KEY` enabled: direct Python social call without key returned `401`, with key returned `200`, and proxied Next route returned `200`.
      - Automated security check returned `ok=true` with burst result counts including both `200` and `429`.
  - Completed Phase 6 acceptance closure:
      - Ran incident-response tabletop checklist for auth misconfig, CORS origin mismatch, and local false-positive rate-limit conditions.
      - Recorded runbook evidence and aligned acceptance artifacts across status/test-plan/metrics/changelog docs.
- Next:
  - Monitor live error-rate and adjust local/production rate-limit profiles if false positives appear.
- Blockers:
  - None.

## Update Template
### YYYY-MM-DD
- Completed:
  - 
- Next:
  - 
- Blockers:
  - 
- Metric movement:
  - 
- Decision links:
  - 
