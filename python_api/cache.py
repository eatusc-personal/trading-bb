"""In-memory TTL cache shared across the API.

A tiny thread-safe {key: (created_ts, value)} store behind get_cached/set_cached.
The heavier stale-while-revalidate layer (SQLite-backed, single-flight) lives in
swr.py.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def get_cached(key: str, ttl_seconds: int) -> Any | None:
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        created, value = entry
        if time.time() - created > ttl_seconds:
            _cache.pop(key, None)
            return None
        return value


def set_cached(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)
