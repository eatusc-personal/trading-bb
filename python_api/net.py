"""Shared HTTP fetch + source cooldown/backoff helpers used by the news and
social layers. Moved verbatim from main.py."""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any
from urllib.request import Request as UrlRequest, urlopen

from .config import *  # noqa: F401,F403 -- user agents, reddit pacing, source fail thresholds

_reddit_rate_lock = threading.Lock()
_reddit_last_request_ts = 0.0
_social_source_state_lock = threading.Lock()
_social_source_failures: dict[str, int] = {}
_social_source_cooldown_until: dict[str, float] = {}


def fetch_json_url(
    url: str,
    timeout: int = 8,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str | None = None,
) -> Any:
    req_headers = {
        "User-Agent": random.choice(SOCIAL_USER_AGENTS),
        "Accept": "application/json,text/plain,*/*",
    }
    if headers:
        req_headers.update(headers)
    req = UrlRequest(
        url,
        headers=req_headers,
        data=data,
        method=method,
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw)

def fetch_text_url(url: str, timeout: int = 8) -> str:
    req = UrlRequest(url, headers={"User-Agent": random.choice(SOCIAL_USER_AGENTS)})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")

def reddit_rate_wait() -> None:
    global _reddit_last_request_ts
    with _reddit_rate_lock:
        now = time.time()
        elapsed = now - _reddit_last_request_ts
        if elapsed < REDDIT_MIN_INTERVAL_SEC:
            time.sleep(REDDIT_MIN_INTERVAL_SEC - elapsed)
        _reddit_last_request_ts = time.time()

def source_cooldown_remaining_sec(source_key: str) -> int:
    now = time.time()
    with _social_source_state_lock:
        until = float(_social_source_cooldown_until.get(source_key, 0.0))
    if until <= now:
        return 0
    return int(max(0, round(until - now)))

def source_record_success(source_key: str) -> None:
    with _social_source_state_lock:
        _social_source_failures[source_key] = 0
        _social_source_cooldown_until[source_key] = 0.0

def source_record_failure(source_key: str) -> tuple[int, int]:
    now = time.time()
    with _social_source_state_lock:
        failures = int(_social_source_failures.get(source_key, 0)) + 1
        _social_source_failures[source_key] = failures
        if failures >= max(1, SOCIAL_SOURCE_FAIL_THRESHOLD):
            cooldown_until = now + max(1, SOCIAL_SOURCE_COOLDOWN_SEC)
            _social_source_cooldown_until[source_key] = cooldown_until
            return failures, int(max(0, round(cooldown_until - now)))
    return failures, 0
