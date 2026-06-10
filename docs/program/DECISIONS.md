# Architecture and Product Decisions

## ADR Template
- ID:
- Date:
- Status: Proposed | Accepted | Superseded
- Context:
- Decision:
- Alternatives considered:
- Consequences:
- Rollback/mitigation:

---

## ADR-001
- ID: ADR-001
- Date: 2026-03-18
- Status: Accepted
- Context: Current social pipeline included Pushshift fallback with compliance/stability risk.
- Decision: Remove Pushshift from production ingestion path and prioritize hardened public Reddit ingestion.
- Alternatives considered: Keep Pushshift as hard fallback; keep optional runtime flag.
- Consequences: Slightly higher implementation effort now, lower compliance and breakage risk later.
- Rollback/mitigation: Dev-only flag can temporarily enable legacy fallback for local debugging.

## ADR-002
- ID: ADR-002
- Date: 2026-03-18
- Status: Accepted
- Context: Quote-only validation is expensive and causes correctness issues for noisy candidates.
- Decision: Promote versioned symbol master to first-pass gate; keep quote validation for liveness only.
- Alternatives considered: Continue quote-first validation.
- Consequences: Faster candidate filtering, better historical reproducibility.
- Rollback/mitigation: Fallback to quote validation if symbol snapshot refresh fails.

## ADR-003
- ID: ADR-003
- Date: 2026-03-18
- Status: Accepted
- Context: OAuth variables are not available for this deployment target, but ingestion still needs resilient behavior.
- Decision: Use low-rate public Reddit listing endpoints with explicit pacing + retry/backoff; use `old.reddit` HTML as emergency fallback.
- Alternatives considered: OAuth-first hybrid; public listing without retry/backoff.
- Consequences: Simpler operations in this environment, with better resilience than naive public polling.
- Rollback/mitigation: If public listing becomes unstable, reduce fetch frequency and rely on cached/stale results with clearer diagnostics.

## ADR-004
- ID: ADR-004
- Date: 2026-03-18
- Status: Accepted
- Context: Phase 3 requires measurable false-positive reduction while keeping explainability and deterministic ranking behavior.
- Decision: Introduce explicit A/B/C candidate tiers, a bounded mention-quality classifier score, and weighted cross-source confirmation features in ranking.
- Alternatives considered: Keep single confidence score only; add black-box model without row-level factor exposure.
- Consequences: Better control over validation costs (A/B priority), clearer explainability payload, and easier holdout benchmarking.
- Rollback/mitigation: Disable tier gating and fall back to confidence-only validation shortlist if severe regressions are observed.

## ADR-005
- ID: ADR-005
- Date: 2026-03-18
- Status: Accepted
- Context: High-buzz social tickers are not always tradable; Phase 4 requires execution-condition awareness and catalyst evidence.
- Decision: Add tradability scoring/gating (liquidity/spread/volatility), market-cap and regime normalization, and lightweight catalyst/options overlays to ranking payloads.
- Alternatives considered: Keep sentiment-only ranking; postpone tradability until full backtest system.
- Consequences: Better ranking robustness under poor execution conditions and more actionable evidence for top picks.
- Rollback/mitigation: If latency regresses, disable per-ticker volatility enrichment beyond top-ranked validation subset.

## ADR-006
- ID: ADR-006
- Date: 2026-03-18
- Status: Accepted
- Context: Phase 5 requires reproducible experiment tracking on ranked social outputs.
- Decision: Persist each social scan as a signal-lab run with row snapshots, model ID, market regime, and optional experiment ID.
- Alternatives considered: Defer persistence until full backtest engine; log only top tickers without full row payloads.
- Consequences: Enables reproducible run inspection and experiment bookkeeping before full event-study runner is built.
- Rollback/mitigation: Reduce stored row count or retention window if storage growth becomes an issue.

## ADR-007
- ID: ADR-007
- Date: 2026-03-18
- Status: Accepted
- Context: Phase 5 requires measurable forward-performance diagnostics on persisted signal runs.
- Decision: Add event-study endpoint by `run_id` with configurable horizons and benchmark, plus a CLI runner script for repeatable reporting.
- Alternatives considered: Require manual notebook analysis; defer event-study until full P&L simulator.
- Consequences: Faster milestone evaluation loops and standardized out-of-sample run summaries.
- Rollback/mitigation: If endpoint latency becomes high, constrain evaluated top-N and horizon set defaults.

## ADR-008
- ID: ADR-008
- Date: 2026-03-18
- Status: Accepted
- Context: Phase 6 requires baseline security controls without breaking local developer velocity.
- Decision: Introduce explicit CORS allowlist, optional social endpoint API-key auth, and in-memory rate limiting for API/social routes.
- Alternatives considered: Keep permissive CORS/no-auth; add external gateway dependency immediately.
- Consequences: Better default security posture with low operational complexity in local deployment.
- Rollback/mitigation: Keep auth optional by env flag and tune/relax rate-limit thresholds if they interfere with development workflows.
