"""Small pure helpers shared across the API: numeric coercion, timestamps,
and OpenBB stderr silencing. No app, config, or provider state."""

from __future__ import annotations

import io
import json
import math
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from typing import Any

import pandas as pd


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_pct(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    pct = float(value)
    if not math.isfinite(pct):
        return None
    if -2 <= pct <= 2:
        pct *= 100
    return pct


def to_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        out = float(value)
        if not math.isfinite(out):
            return None
        return out
    except Exception:
        return None


def call_openbb_silenced(fn: Any, *args: Any, **kwargs: Any) -> Any:
    # OpenBB/yfinance can emit noisy stderr lines (e.g., Invalid Crumb) on
    # upstream auth failures. Capture them to keep API logs actionable.
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        return fn(*args, **kwargs)


def finite_or(value: Any, fallback: float = 0.0) -> float:
    parsed = to_float(value)
    return parsed if parsed is not None else float(fallback)


def parse_iso_dt(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_json_array(value: str | None) -> list[dict[str, Any]]:
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return []
    except Exception:
        return []
