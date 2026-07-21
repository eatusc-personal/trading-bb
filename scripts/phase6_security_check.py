#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(url: str, *, headers: dict[str, str] | None = None, timeout: int = 30) -> tuple[int, dict[str, str], str]:
    req = Request(url, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = int(resp.getcode() or 0)
            body = resp.read().decode("utf-8", errors="ignore")
            h = {str(k).lower(): str(v) for k, v in resp.headers.items()}
            return status, h, body
    except HTTPError as exc:
        status = int(exc.code or 0)
        body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        h = {str(k).lower(): str(v) for k, v in (exc.headers.items() if exc.headers else [])}
        return status, h, body


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 6 security hardening verification.")
    parser.add_argument("--web-base", default="http://127.0.0.1:3000", help="Next API base URL")
    parser.add_argument("--api-base", default="http://127.0.0.1:8001", help="Python API base URL")
    parser.add_argument("--api-key", default="", help="Social API key for auth-enabled checks")
    parser.add_argument("--burst", type=int, default=135, help="Burst request count for rate-limit verification")
    parser.add_argument("--timeout", type=int, default=30, help="Request timeout seconds")
    args = parser.parse_args()

    web = args.web_base.rstrip("/")
    api = args.api_base.rstrip("/")
    api_key = str(args.api_key or "").strip()

    results: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "web_base": web,
        "api_base": api,
        "checks": {},
    }
    checks: dict[str, object] = results["checks"]  # type: ignore[assignment]

    # 1) Health (Python API authoritative; Next proxy may not expose /api/health directly).
    s, _h, b = request(f"{api}/api/health", timeout=max(5, args.timeout))
    checks["health"] = {"status": s, "ok": s == 200}
    if s != 200:
        checks["health"]["body"] = b[:200]  # type: ignore[index]

    # 2) Security status
    s, _h, b = request(f"{web}/api/security/status", timeout=max(5, args.timeout))
    sec_payload = {}
    try:
        sec_payload = json.loads(b) if b else {}
    except Exception:
        sec_payload = {}
    checks["security_status"] = {
        "status": s,
        "ok": s == 200 and isinstance(sec_payload, dict),
        "payload": sec_payload,
    }

    # 3) Direct social auth behavior
    s_no_key, _h_no_key, b_no_key = request(f"{api}/api/social/sentiment?limit=5", timeout=max(10, args.timeout))
    direct: dict[str, object] = {"without_key_status": s_no_key}
    if api_key:
        s_with_key, _h_with_key, _b_with_key = request(
            f"{api}/api/social/sentiment?limit=5",
            headers={"x-api-key": api_key},
            timeout=max(10, args.timeout),
        )
        direct["with_key_status"] = s_with_key
        direct["auth_ok"] = s_no_key == 401 and s_with_key == 200
    else:
        direct["auth_ok"] = s_no_key == 200
    if s_no_key not in {200, 401}:
        direct["without_key_body"] = b_no_key[:200]
    checks["direct_social_auth"] = direct

    # 4) Proxy social should work when key forwarding is configured.
    s_web_social, _h_web_social, b_web_social = request(f"{web}/api/social/sentiment?limit=5", timeout=max(10, args.timeout))
    checks["proxied_social"] = {
        "status": s_web_social,
        "ok": s_web_social == 200,
        "sample": b_web_social[:180],
    }

    # 5) Burst for 429 + Retry-After evidence.
    code_counts: dict[str, int] = {}
    saw_retry_after = False
    retry_after_value = None
    for _ in range(max(1, int(args.burst))):
        s_code, s_headers, _ = request(f"{web}/api/social/sentiment?limit=5", timeout=max(10, args.timeout))
        code_counts[str(s_code)] = int(code_counts.get(str(s_code), 0)) + 1
        if s_code == 429:
            ra = s_headers.get("retry-after")
            if ra:
                saw_retry_after = True
                retry_after_value = ra

    checks["burst_rate_limit"] = {
        "requests": int(args.burst),
        "code_counts": code_counts,
        "has_429": int(code_counts.get("429", 0)) > 0,
        "retry_after_seen": saw_retry_after,
        "retry_after_sample": retry_after_value,
    }

    print(json.dumps(results, indent=2))

    ok = (
        bool(checks.get("health", {}).get("ok"))  # type: ignore[union-attr]
        and bool(checks.get("security_status", {}).get("ok"))  # type: ignore[union-attr]
        and bool(checks.get("direct_social_auth", {}).get("auth_ok"))  # type: ignore[union-attr]
        and bool(checks.get("proxied_social", {}).get("ok"))  # type: ignore[union-attr]
        and bool(checks.get("burst_rate_limit", {}).get("has_429"))  # type: ignore[union-attr]
        and bool(checks.get("burst_rate_limit", {}).get("retry_after_seen"))  # type: ignore[union-attr]
    )

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    print(
        "\nMarkdown:\n"
        f"- {ts}: phase6_security_check ok={str(ok).lower()}, "
        f"codes={code_counts}, "
        f"auth_ok={checks.get('direct_social_auth', {}).get('auth_ok')}, "
        f"retry_after_seen={saw_retry_after}."
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
