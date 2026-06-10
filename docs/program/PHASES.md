# Phase Definitions

## Phase 1: Reliability + Compliance Foundation
### Scope
- Remove Pushshift from production social ingestion.
- Implement Reddit public-ingestion hardening with compliant user-agent, pacing, retry, and backoff.
- Introduce source health counters, retries, backoff, and circuit-breaker behavior.
### Deliverables
- Updated ingestion pipeline and configuration.
- Health telemetry fields per source in API diagnostics.
- Fallback policy doc for source outage scenarios.
### Exit Criteria
- Scanner works without Pushshift.
- Public Reddit ingestion path stable under normal refresh load.
- Source failures degrade gracefully without full endpoint failure.

## Phase 2: Data Foundation + Symbol Master
### Scope
- Versioned symbol-master snapshots from primary sources.
- Add symbol alias handling and historical validity lookups.
- Split symbol-validity persistence from quote-liveness caching.
### Deliverables
- Snapshot schema and daily refresh process.
- Symbol lookup API/utility contracts with effective date.
- Persistent cache keys with provenance and timestamp metadata.
### Exit Criteria
- Historical runs reproduce symbol validity for a given date.
- Reduced quote-validation calls due to symbol master pre-gating.

## Phase 3: Signal Quality Core
### Scope
- Implement tiered candidate generation rules (A/B/C confidence tiers).
- Add calibrated mention-quality classifier and confidence thresholds.
- Upgrade cross-source confirmation to weighted signal features.
### Deliverables
- Candidate scoring service with explainability outputs.
- Labeled sample process and evaluation notes.
- Updated ranking formula including confidence penalties/bonuses.
### Exit Criteria
- False-positive rate reduced vs baseline.
- Precision@k uplift on holdout period.

## Phase 4: Tradability + Alpha Features
### Scope
- Add liquidity/spread/volatility gating.
- Add catalyst overlays (filings/events) and options context where available.
- Add market-cap and regime normalization controls.
### Deliverables
- Tradability feature set embedded in ranking payload.
- Catalyst evidence objects for top signals.
- Downranking rules for poor execution conditions.
### Exit Criteria
- Ranked outputs include both sentiment and tradability context.
- Backtests show improved risk-adjusted metrics vs buzz-only baseline.

## Phase 5: Signal Lab + Evaluation
### Scope
- Persist scan feature vectors and ranked outputs by timestamp.
- Build event-study and P&L simulation harness.
- Add experiment/version tracking and dashboards.
### Deliverables
- Signal lab data model and experiment runner.
- Standard metric outputs (precision@k, hit rate, Sharpe, drawdown).
- Reproducibility guide and experiment ID discipline.
### Exit Criteria
- Model changes are compared by reproducible experiments.
- Out-of-sample performance reporting is automated.

## Phase 6: Hardening + Productization
### Scope
- Security hardening (CORS restrictions, auth path, rate limits).
- Reliability hardening (load tests, runbooks, alerting).
- UI evidence drilldowns and playbook-style output framing.
### Deliverables
- Security policy + deployment guardrails.
- Operational runbook and incident response checklist.
- Final acceptance report with KPI outcomes.
### Exit Criteria
- Operational readiness checklist complete.
- Program KPIs reviewed and signed off.
