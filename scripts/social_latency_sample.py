#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from datetime import UTC, datetime
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * p
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return values[lo]
    weight = rank - lo
    return values[lo] * (1 - weight) + values[hi] * weight


def fetch_json(url: str, timeout: int) -> tuple[int, dict]:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "openbb-latency-sampler/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        status = int(resp.getcode() or 0)
        raw = resp.read().decode("utf-8", errors="ignore")
    payload = json.loads(raw) if raw else {}
    return status, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample social sentiment endpoint latency and print summary.")
    parser.add_argument("--url", default="http://127.0.0.1:3000/api/social/sentiment?limit=5", help="Endpoint URL")
    parser.add_argument("--samples", type=int, default=5, help="Number of requests")
    parser.add_argument("--timeout", type=int, default=90, help="Per-request timeout seconds")
    parser.add_argument("--pause", type=float, default=0.2, help="Pause between requests (seconds)")
    args = parser.parse_args()

    samples = max(1, args.samples)
    latencies: list[float] = []
    statuses: list[int] = []
    last_mode = "-"
    last_provider_unavailable = False
    last_count = 0
    failures = 0

    for i in range(samples):
        t0 = time.perf_counter()
        try:
            status, payload = fetch_json(args.url, args.timeout)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)
            statuses.append(status)
            vd = payload.get("validation_diagnostics") or {}
            last_mode = str(vd.get("mode") or "-")
            last_provider_unavailable = bool(vd.get("provider_unavailable") or False)
            last_count = int(payload.get("count") or 0)
            print(f"[sample {i + 1}/{samples}] status={status} latency_ms={elapsed_ms:.1f} count={last_count}")
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, Exception) as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)
            statuses.append(0)
            failures += 1
            print(f"[sample {i + 1}/{samples}] status=ERR latency_ms={elapsed_ms:.1f} error={exc}")
        if i < samples - 1 and args.pause > 0:
            time.sleep(args.pause)

    ordered = sorted(latencies)
    p50 = percentile(ordered, 0.50)
    p95 = percentile(ordered, 0.95)
    avg = statistics.mean(latencies)

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    status_set = sorted(set(statuses))
    print("\n---")
    print(f"timestamp_utc={ts}")
    print(f"url={args.url}")
    print(f"samples={samples}")
    print(f"failures={failures}")
    print(f"status_codes={status_set}")
    print(f"latency_ms_avg={avg:.1f}")
    print(f"latency_ms_p50={p50:.1f}")
    print(f"latency_ms_p95={p95:.1f}")
    print(f"validation_mode_last={last_mode}")
    print(f"provider_unavailable_last={str(last_provider_unavailable).lower()}")
    print(f"result_count_last={last_count}")
    print("\nMarkdown:")
    print(f"- {ts}: samples={samples}, failures={failures}, p50={p50:.1f}ms, p95={p95:.1f}ms, mode={last_mode}, provider_unavailable={str(last_provider_unavailable).lower()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

