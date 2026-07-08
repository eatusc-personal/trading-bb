# Risk Register

> This is a solo project. "Owner" below is the single maintainer wearing
> different hats (backend, quant, platform), not a team.

## Active Risks

### R-001: Ingestion compliance drift
- Category: Compliance
- Severity: High
- Likelihood: Medium
- Description: Source access terms or enforcement may change and break ingestion.
- Mitigation: public-endpoint pacing/retry discipline, source policy checks, no production Pushshift.
- Owner: Maintainer

### R-002: Source outage concentration
- Category: Reliability
- Severity: High
- Likelihood: Medium
- Description: Single-source outages could degrade rankings.
- Mitigation: Graceful degradation, source health telemetry, circuit breakers.
- Owner: Maintainer

### R-003: False-positive ticker extraction
- Category: Model quality
- Severity: High
- Likelihood: High
- Description: Ambiguous tokens can leak into ranking output.
- Mitigation: Tiered gating, classifier, symbol master, ambiguity penalties.
- Owner: Maintainer

### R-004: Backtest overfitting
- Category: Research validity
- Severity: High
- Likelihood: Medium
- Description: Rules optimized to in-sample behavior may not generalize.
- Mitigation: Holdout windows, regime slicing, experiment tracking discipline.
- Owner: Maintainer

### R-005: Performance regression on heavy endpoints
- Category: Performance
- Severity: Medium
- Likelihood: Medium
- Description: Additional features may increase latency.
- Mitigation: layered caching, async fetch strategy, profiling before release.
- Owner: Maintainer

## Risk Review Cadence
- Review active risks weekly.
- Update status/owner after each milestone.
- Escalate unresolved High severity risks before phase close.
