#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from urllib.request import Request, urlopen


def fetch_json(url: str, timeout: int = 90) -> dict:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "openbb-phase5-event-study/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw) if raw else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 5 event-study runner for persisted social signal runs.")
    parser.add_argument("--base", default="http://127.0.0.1:3000", help="Base URL")
    parser.add_argument("--run-id", default="", help="Signal run ID (defaults to latest)")
    parser.add_argument("--horizons", default="1,3,5", help="Comma-separated horizon days")
    parser.add_argument("--benchmark", default="SPY", help="Benchmark symbol")
    parser.add_argument("--top", type=int, default=20, help="Top ranked rows to evaluate")
    parser.add_argument("--timeout", type=int, default=90, help="Request timeout seconds")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    run_id = str(args.run_id or "").strip()
    if not run_id:
        latest = fetch_json(f"{base}/api/social/signal-lab/runs?limit=1", timeout=max(10, int(args.timeout)))
        rows = latest.get("rows") or []
        if not rows:
            raise SystemExit("no persisted signal runs found; call /api/social/sentiment first")
        run_id = str((rows[0] or {}).get("run_id") or "")
    if not run_id:
        raise SystemExit("unable to resolve run_id")

    url = (
        f"{base}/api/social/signal-lab/event-study/{run_id}"
        f"?horizons={args.horizons}&benchmark={args.benchmark}&top={max(1, int(args.top))}"
    )
    payload = fetch_json(url, timeout=max(10, int(args.timeout)))
    run = payload.get("run") or {}
    summary = payload.get("summary") or {}

    out = {
        "timestamp": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "run_created_at": run.get("created_at"),
        "model_id": run.get("model_id"),
        "experiment_id": run.get("experiment_id"),
        "market_regime": run.get("market_regime"),
        "benchmark": payload.get("benchmark"),
        "horizons_days": payload.get("horizons_days") or [],
        "count": int(payload.get("count") or 0),
        "summary": summary,
    }
    print(json.dumps(out, indent=2))

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    parts: list[str] = []
    for k in sorted(summary.keys()):
        item = summary.get(k) or {}
        hr = item.get("hit_rate")
        alpha = item.get("avg_alpha_pct")
        if hr is None:
            continue
        parts.append(f"{k}: hit_rate={hr}, avg_alpha_pct={alpha}")
    detail = "; ".join(parts) if parts else "no summary rows"
    print(f"\nMarkdown:\n- {ts}: phase5_event_study run_id={run_id}, benchmark={payload.get('benchmark')}, {detail}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
