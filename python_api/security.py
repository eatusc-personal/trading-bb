"""Request guards: client IP resolution, the optional social API key, and the
per-IP fixed-window rate limiter used by the HTTP middleware in main.py."""

from __future__ import annotations

import math
import os
import threading
import time

from fastapi import HTTPException, Request as FastAPIRequest

from .config import SOCIAL_API_KEY


_rate_limit_lock = threading.Lock()
_rate_limit_state: dict[str, tuple[float, int]] = {}



# Only trust X-Forwarded-For when explicitly running behind a proxy that
# OVERWRITES it. Otherwise any client can spoof the header to rotate its
# apparent IP and defeat the per-IP rate limiter.
TRUST_FORWARDED_FOR = os.getenv("TRUST_FORWARDED_FOR", "0").strip().lower() not in ("", "0", "false", "no")


def client_ip(request: FastAPIRequest) -> str:
    if TRUST_FORWARDED_FOR:
        forwarded = str(request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return str(request.client.host)
    return "unknown"


def enforce_social_api_key(request: FastAPIRequest) -> None:
    if not SOCIAL_API_KEY:
        return
    header_key = str(request.headers.get("x-api-key") or "").strip()
    auth = str(request.headers.get("authorization") or "").strip()
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    supplied = header_key or bearer
    if supplied != SOCIAL_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


def rate_limit_check(scope_key: str, subject_key: str, max_requests: int, window_sec: int) -> tuple[bool, int]:
    now = time.time()
    key = f"{scope_key}:{subject_key}"
    with _rate_limit_lock:
        started, count = _rate_limit_state.get(key, (now, 0))
        if now - started >= max(1, window_sec):
            started, count = now, 0
        count += 1
        _rate_limit_state[key] = (started, count)
        remaining = max(0, int(max_requests) - count)
        if count > int(max_requests):
            retry_after = max(1, int(math.ceil(max(0.0, window_sec - (now - started)))))
            return False, retry_after
        return True, remaining
