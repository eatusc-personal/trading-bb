"""Health, security status and provider budget endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import provider_budget
from ..config import (
    API_RATE_LIMIT_MAX_REQUESTS,
    API_RATE_LIMIT_WINDOW_SEC,
    CORS_ALLOW_ORIGINS,
    ENABLE_YFINANCE,
    FMP_API_KEY,
    HISTORY_PROVIDER_PRIORITY,
    MARKETSTACK_API_KEY,
    MARKET_DATA_PROVIDER,
    QUOTE_PROVIDER_PRIORITY,
    SOCIAL_API_KEY,
    SOCIAL_RATE_LIMIT_MAX_REQUESTS,
    SOCIAL_RATE_LIMIT_WINDOW_SEC,
    TWELVE_DATA_API_KEY,
)
from ..providers import resolve_runtime_priorities
from ..utils import now_iso

router = APIRouter()


@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": now_iso(), "provider": MARKET_DATA_PROVIDER}


@router.get("/api/security/status")
def security_status() -> dict[str, Any]:
    return {
        "updated_at": now_iso(),
        "cors_allow_origins": CORS_ALLOW_ORIGINS,
        "social_api_key_configured": bool(SOCIAL_API_KEY),
        "market_data": {
            "provider": MARKET_DATA_PROVIDER,
            "keys_configured": {
                "twelvedata": bool(TWELVE_DATA_API_KEY),
                "fmp": bool(FMP_API_KEY),
                "marketstack": bool(MARKETSTACK_API_KEY),
                "yfinance": ENABLE_YFINANCE,
            },
            "quote_priority": resolve_runtime_priorities(QUOTE_PROVIDER_PRIORITY, ("twelvedata", "fmp", "marketstack")),
            "history_priority": resolve_runtime_priorities(HISTORY_PROVIDER_PRIORITY, ("twelvedata", "fmp", "marketstack")),
        },
        "rate_limit": {
            "api": {"window_sec": API_RATE_LIMIT_WINDOW_SEC, "max_requests": API_RATE_LIMIT_MAX_REQUESTS},
            "social": {"window_sec": SOCIAL_RATE_LIMIT_WINDOW_SEC, "max_requests": SOCIAL_RATE_LIMIT_MAX_REQUESTS},
        },
        "provider_budget": provider_budget.snapshot(),
    }


@router.get("/api/providers/budget")
def providers_budget() -> dict[str, Any]:
    return {"updated_at": now_iso(), **provider_budget.snapshot()}
