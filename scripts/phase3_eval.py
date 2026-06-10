#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen


def fetch_json(url: str, timeout: int = 60) -> dict:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "openbb-phase3-eval/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw) if raw else {}


def precision_at_k(ranked: list[str], labels: dict[str, int], k: int) -> float:
    subset = ranked[: max(0, k)]
    if not subset:
        return 0.0
    positives = sum(1 for sym in subset if int(labels.get(sym, 0)) == 1)
    return positives / len(subset)


def false_positive_rate(ranked: list[str], labels: dict[str, int], k: int) -> float:
    subset = ranked[: max(0, k)]
    if not subset:
        return 0.0
    false_positives = sum(1 for sym in subset if int(labels.get(sym, 0)) == 0)
    return false_positives / len(subset)


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0 if new == 0 else 100.0
    return ((new - old) / abs(old)) * 100.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 holdout evaluation: false-positive rate + precision@k.")
    parser.add_argument("--base", default="http://127.0.0.1:3000", help="Base URL")
    parser.add_argument("--limit", type=int, default=20, help="Top N rows to evaluate")
    parser.add_argument("--benchmark", default="docs/program/PHASE3_BENCHMARK.json", help="Benchmark JSON path")
    parser.add_argument("--timeout", type=int, default=90, help="Request timeout in seconds")
    args = parser.parse_args()

    benchmark_path = Path(args.benchmark)
    if not benchmark_path.exists():
        raise SystemExit(f"benchmark file not found: {benchmark_path}")

    bench = json.loads(benchmark_path.read_text(encoding="utf-8"))
    labels = {str(k).upper(): int(v) for k, v in (bench.get("holdout_labels") or {}).items()}
    baseline_ranked = [str(x).upper() for x in (bench.get("baseline_ranked") or [])]
    baseline_model_id = str(bench.get("baseline_model_id") or "social_v1")
    if not labels:
        raise SystemExit("benchmark holdout_labels is empty")

    base = args.base.rstrip("/")
    payload = fetch_json(f"{base}/api/social/sentiment?limit={max(5, int(args.limit))}", timeout=max(10, int(args.timeout)))
    current_ranked = [str(row.get("ticker") or "").upper() for row in (payload.get("rows") or []) if row.get("ticker")]
    current_model_id = str(payload.get("model_id") or "unknown")

    ks = [5, 10, 20]
    metrics = {
        "baseline": {},
        "current": {},
        "uplift": {},
    }
    for k in ks:
        baseline_p = precision_at_k(baseline_ranked, labels, k)
        current_p = precision_at_k(current_ranked, labels, k)
        baseline_fpr = false_positive_rate(baseline_ranked, labels, k)
        current_fpr = false_positive_rate(current_ranked, labels, k)
        metrics["baseline"][f"precision_at_{k}"] = round(baseline_p, 4)
        metrics["current"][f"precision_at_{k}"] = round(current_p, 4)
        metrics["uplift"][f"precision_at_{k}_pct"] = round(pct_change(baseline_p, current_p), 2)
        metrics["baseline"][f"false_positive_rate_at_{k}"] = round(baseline_fpr, 4)
        metrics["current"][f"false_positive_rate_at_{k}"] = round(current_fpr, 4)
        metrics["uplift"][f"false_positive_reduction_at_{k}_pct"] = round((baseline_fpr - current_fpr) * 100.0, 2)

    out = {
        "timestamp": datetime.now(UTC).isoformat(),
        "benchmark_file": str(benchmark_path),
        "baseline_model_id": baseline_model_id,
        "current_model_id": current_model_id,
        "evaluated_limit": int(args.limit),
        "metrics": metrics,
        "tier_counts": payload.get("tier_counts") or {},
        "validation_mode": ((payload.get("validation_diagnostics") or {}).get("mode") or ""),
    }
    print(json.dumps(out, indent=2))
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    print(
        "\nMarkdown:\n"
        f"- {ts}: phase3_eval baseline={baseline_model_id}, current={current_model_id}, "
        f"p10={metrics['current'].get('precision_at_10')}, "
        f"fpr10={metrics['current'].get('false_positive_rate_at_10')}, "
        f"fpr_reduction10={metrics['uplift'].get('false_positive_reduction_at_10_pct')}pp."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
