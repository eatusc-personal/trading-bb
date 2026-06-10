#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from urllib.request import Request, urlopen


def fetch_json(url: str, timeout: int = 30) -> dict:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "openbb-phase2-check/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw) if raw else {}


def stable_resolution_signature(payload: dict) -> str:
    res = payload.get("resolution") or {}
    parts = [
        str(payload.get("as_of_date") or ""),
        str(payload.get("snapshot_date") or ""),
        str(res.get("input_symbol") or ""),
        str(res.get("resolved_symbol") or ""),
        str(bool(res.get("is_valid") or False)),
        str(bool(res.get("alias_applied") or False)),
    ]
    return "|".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 reproducibility check for symbol master resolver.")
    parser.add_argument("--base", default="http://127.0.0.1:3000", help="Base URL")
    parser.add_argument("--as-of", default=datetime.now(UTC).date().isoformat(), help="As-of date YYYY-MM-DD")
    parser.add_argument("--runs", type=int, default=3, help="Repeated runs per symbol")
    parser.add_argument(
        "--symbols",
        default="AAPL,FB,SQ,BRK-B",
        help="Comma-separated symbols to test",
    )
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    runs = max(2, int(args.runs))
    as_of = str(args.as_of)
    base = args.base.rstrip("/")

    health = fetch_json(f"{base}/api/social/symbol-master/health")
    snapshots = fetch_json(f"{base}/api/social/symbol-master/snapshots?limit=5")
    alias_health = fetch_json(f"{base}/api/social/symbol-master/alias/health")

    deterministic = True
    rows: list[dict] = []
    for symbol in symbols:
        signatures: list[str] = []
        payloads: list[dict] = []
        for _ in range(runs):
            p = fetch_json(f"{base}/api/social/symbol-master/resolve?symbol={symbol}&as_of={as_of}")
            payloads.append(p)
            signatures.append(stable_resolution_signature(p))
            time.sleep(0.05)
        ok = len(set(signatures)) == 1
        if not ok:
            deterministic = False
        first = payloads[0] if payloads else {}
        rows.append(
            {
                "symbol": symbol,
                "deterministic": ok,
                "signature": signatures[0] if signatures else "",
                "resolved_symbol": (((first.get("resolution") or {}).get("resolved_symbol")) or ""),
                "is_valid": bool(((first.get("resolution") or {}).get("is_valid")) or False),
                "alias_applied": bool(((first.get("resolution") or {}).get("alias_applied")) or False),
                "snapshot_date": str(first.get("snapshot_date") or ""),
            }
        )

    output = {
        "timestamp": datetime.now(UTC).isoformat(),
        "base": base,
        "as_of": as_of,
        "runs": runs,
        "deterministic": deterministic,
        "symbol_master_health": {
            "current_snapshot_date": health.get("current_snapshot_date"),
            "snapshot_count": health.get("snapshot_count"),
            "current_snapshot_symbol_count": health.get("current_snapshot_symbol_count"),
        },
        "alias_health": {
            "alias_count": alias_health.get("alias_count"),
            "last_refreshed_on": alias_health.get("last_refreshed_on"),
        },
        "snapshots": snapshots.get("rows") or [],
        "rows": rows,
    }
    print(json.dumps(output, indent=2))
    print(
        f"\nMarkdown:\n- {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%SZ')}: "
        f"phase2_repro deterministic={str(deterministic).lower()}, symbols={len(symbols)}, runs={runs}, "
        f"snapshot_date={health.get('current_snapshot_date')}, alias_count={alias_health.get('alias_count')}."
    )
    return 0 if deterministic else 2


if __name__ == "__main__":
    raise SystemExit(main())
