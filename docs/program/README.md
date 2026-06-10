# Social-Sentiment Scanner Program

## Program Charter
This program upgrades the social-sentiment scanner from a local dashboard feature into a reliable, compliance-aware, testable alpha research system.

## Primary Goals
- Improve social signal precision and reduce ticker false positives.
- Increase ingestion reliability and remove high-risk dependencies.
- Make model changes measurable with reproducible evaluation.
- Add tradability and catalyst context so rankings are actionable.

## Non-Goals (for this program)
- Building a fully managed cloud platform.
- Expanding into every social source before core quality gates are stable.
- Treating headline UI polish as a substitute for signal validation.

## Success KPIs
- False-positive ticker rate reduced by >= 40% vs baseline.
- Social endpoint p95 latency under target for warm-cache calls.
- Precision@k and hit-rate improvements on out-of-sample windows.
- Every ranking row has explainability fields and model/version metadata.

## Program Structure
- Roadmap and dependencies: `ROADMAP.md`
- Active tracking and daily updates: `STATUS.md`
- Phase requirements and exits: `PHASES.md`
- Decisions and tradeoffs: `DECISIONS.md`
- Metrics and experiments: `METRICS.md`
- Test matrix and acceptance gates: `TEST_PLAN.md`
- Risks and mitigations: `RISK_REGISTER.md`
- Milestone release notes: `CHANGELOG.md`
- Symbol-master refresh and diagnostics runbook: `SYMBOL_MASTER_RUNBOOK.md`
- Phase 3 benchmark seed file: `PHASE3_BENCHMARK.json`
- Security hardening runbook: `SECURITY_RUNBOOK.md`

## Working Rules
- Update `STATUS.md` daily while a phase is active.
- Update `DECISIONS.md` for any architectural/product tradeoff.
- Do not close a phase without linked evidence in `METRICS.md` and `TEST_PLAN.md`.
- Keep implementation scoped to current phase unless blocker requires exception.
