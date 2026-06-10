# Program Changelog

## 2026-03-18
### Added
- Program documentation system under `docs/program`.
- 6-phase roadmap with dependencies and milestone gates.
- Daily status cadence and decision-log templates.
- Initial metrics, test matrix, and risk register.

### Changed
- Social ingestion path now uses hardened public Reddit listing fetches and no longer calls Pushshift in production flow.
- Added Reddit pacing controls and public endpoint retry/backoff behavior.
- Added source-health diagnostics in social sentiment API response, including retry/timing and cooldown-skip telemetry.
- Removed OAuth-based Reddit ingestion path from runtime implementation.
- Added Source Health table on Social dashboard page.
- Added symbol-master snapshot persistence (SQLite) with response metadata (`symbol_master_snapshot_date`, `symbol_master_source`).
- Added historical symbol resolution endpoint (`/api/social/symbol-master/resolve`) with `as_of` lookup support.
- Added persistent symbol validity cache (`symbol_validity_cache`) and split quote liveness into dedicated short-lived cache (`quote_liveness_cache` + in-memory 60s TTL).
- Added seed-driven alias ingestion (`python_api/symbol_alias_seed.json`) with daily refresh policy and manual refresh API.
- Added alias diagnostics/ops endpoints (`/api/social/symbol-master/alias/health`, `POST /api/social/symbol-master/alias/refresh`).
- Expanded alias seed mappings from 11 to 20 entries.
- Added snapshot-list endpoint (`/api/social/symbol-master/snapshots`) for historical reproducibility diagnostics.
- Added reproducibility check runner (`scripts/phase2_repro_check.py`) and npm task (`metrics:phase2-repro`).
- Added optional TikTok provider connector hook (`TIKTOK_PROVIDER_URL` + `TIKTOK_PROVIDER_API_KEY`) with no scraping path.
- Added symbol-master runbook (`docs/program/SYMBOL_MASTER_RUNBOOK.md`) and linked it in program docs.
- Implemented Phase 3 tiered candidate quality flow in `/api/social/sentiment`:
  - confidence tiers `A/B/C`
  - mention-quality classifier score (`mention_quality_score`)
  - weighted cross-source confirmation score (`weighted_source_score`)
  - explainability object and payload model metadata (`model_id`)
- Updated Social dashboard table with tier/quality/source-weight columns.
- Added Phase 3 holdout evaluation harness (`scripts/phase3_eval.py`) and npm task (`metrics:phase3-eval`).
- Added benchmark seed file for evaluation bootstrap (`docs/program/PHASE3_BENCHMARK.json`).
- Added Phase 4 tradability/alpha overlays in `/api/social/sentiment`:
  - tradability scoring + gating from liquidity, spread, annualized volatility
  - market-cap normalization and market-regime multiplier (`risk_on`/`risk_off`/`mixed`)
  - catalyst tags from mention text and options-context intensity score
  - row payload fields for tradability diagnostics and catalyst evidence
- Updated scoring model ID to `social_v4_tradability_context`.
- Updated Social page ranked table to surface tradability score and catalyst tags.
- Added Phase 5 signal-lab persistence scaffold:
  - SQLite tables `social_signal_run` and `social_signal_row`
  - automatic signal-run persistence from `/api/social/sentiment` with `signal_run_id`
  - optional `experiment_id` passthrough on social sentiment scans
  - retrieval APIs `/api/social/signal-lab/runs` and `/api/social/signal-lab/run/{run_id}`
  - Next proxy routes for signal-lab endpoints
- Added Phase 5 event-study runner capability:
  - API endpoint `/api/social/signal-lab/event-study/{run_id}` for horizon return/hit-rate/alpha summaries
  - Next proxy route for event-study endpoint
  - script `scripts/phase5_event_study.py` and npm task `metrics:phase5-event-study`
- Added Phase 6 hardening baseline:
  - restricted CORS default origins (configurable via `CORS_ALLOW_ORIGINS`)
  - optional social API key auth guard via `SOCIAL_API_KEY`
  - API/social in-memory rate limiting with `429` and `Retry-After`
  - security diagnostics endpoint `/api/security/status`
  - security hardening runbook `docs/program/SECURITY_RUNBOOK.md`
  - automated checker script `scripts/phase6_security_check.py` and npm task `metrics:phase6-security`
- Fixed HTTP request type alias collision between FastAPI `Request` and `urllib.request.Request` that affected social ingestion fetches.
- Updated Next proxy to pass through upstream `retry-after` header for rate-limited responses.
- Completed Phase 6 acceptance evidence:
  - incident-response tabletop scenarios documented in runbook and test artifacts
  - status/metrics/test-plan updated to reflect Phase 6 completion

### Notes
- Phase 1 implementation has started in code paths.
