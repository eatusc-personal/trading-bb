# Program Roadmap

## Timeline
- Start date: 2026-03-18
- Cadence: daily progress logs + milestone checkpoints
- Phases: 6

## Phase Plan
1. Phase 1: Reliability + Compliance Foundation
- Target window: 2026-03-18 to 2026-03-31
- Gate: Pushshift removed from production path; Reddit ingestion is public-endpoint based with strict pacing/retry governance.

2. Phase 2: Data Foundation + Symbol Master
- Target window: 2026-04-01 to 2026-04-14
- Gate: versioned symbol master snapshots live with historical reproducibility.

3. Phase 3: Signal Quality Core
- Target window: 2026-04-15 to 2026-04-30
- Gate: tiered candidate generation + classifier materially reduce false positives.

4. Phase 4: Tradability and Alpha Features
- Target window: 2026-05-01 to 2026-05-20
- Gate: rankings include liquidity/volatility/catalyst context and gating.

5. Phase 5: Signal Lab + Evaluation
- Target window: 2026-05-21 to 2026-06-15
- Gate: reproducible backtests and experiment comparisons are operational.

6. Phase 6: Hardening + Productization
- Target window: 2026-06-16 to 2026-07-05
- Gate: security controls, runbooks, and load reliability targets met.

## Dependencies
- Phase 2 depends on Phase 1 ingestion contract stability.
- Phase 3 depends on Phase 2 symbol master and persisted features.
- Phase 4 depends on Phase 3 ranking core.
- Phase 5 depends on Phases 2-4 data contracts.
- Phase 6 depends on Phase 5 observability and metrics.

## Milestone Checklist
- M1: Ingestion compliance and reliability baseline complete.
- M2: Versioned symbol registry and cache split complete.
- M3: Candidate quality uplift complete.
- M4: Tradability/catalyst features integrated.
- M5: Signal lab metrics accepted.
- M6: Production-hardening sign-off complete.
