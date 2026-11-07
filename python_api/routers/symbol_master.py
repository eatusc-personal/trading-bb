"""Symbol-master endpoints under /api/social/symbol-master."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request as FastAPIRequest

from ..market_data import validate_symbol
from ..security import enforce_social_api_key
from ..symbol_master import (
    load_symbol_master_at_date,
    normalize_as_of_date,
    refresh_symbol_alias_seed,
    resolve_symbol_validity,
    symbol_alias_health,
    symbol_master_health,
    symbol_master_snapshots,
)
from ..utils import now_iso

router = APIRouter()


@router.get("/api/social/symbol-master/health")
def social_symbol_master_health(request: FastAPIRequest) -> dict[str, Any]:
    enforce_social_api_key(request)
    return symbol_master_health()


@router.get("/api/social/symbol-master/snapshots")
def social_symbol_master_snapshots(request: FastAPIRequest, limit: int = Query(default=60, ge=1, le=365)) -> dict[str, Any]:
    enforce_social_api_key(request)
    return symbol_master_snapshots(limit)


@router.get("/api/social/symbol-master/alias/health")
def social_symbol_alias_health(request: FastAPIRequest) -> dict[str, Any]:
    enforce_social_api_key(request)
    return symbol_alias_health()


@router.post("/api/social/symbol-master/alias/refresh")
def social_symbol_alias_refresh(request: FastAPIRequest) -> dict[str, Any]:
    enforce_social_api_key(request)
    result = refresh_symbol_alias_seed(force=True)
    return {"updated_at": now_iso(), **result}


@router.get("/api/social/symbol-master/resolve")
def social_symbol_master_resolve(
    request: FastAPIRequest,
    symbol: str = Query(..., min_length=1, max_length=12),
    as_of: str | None = Query(default=None, description="ISO date (YYYY-MM-DD). Defaults to today."),
) -> dict[str, Any]:
    enforce_social_api_key(request)
    normalized = validate_symbol(symbol)
    as_of_date = normalize_as_of_date(as_of)
    validity = resolve_symbol_validity(normalized, as_of_date)
    # Also return snapshot-level context for reproducibility.
    symbol_master, snapshot_date, snapshot_source = load_symbol_master_at_date(as_of_date)
    return {
        "updated_at": now_iso(),
        "input_symbol": normalized,
        "as_of_date": as_of_date,
        "snapshot_date": snapshot_date,
        "snapshot_source": snapshot_source,
        "symbol_master_count": len(symbol_master),
        "resolution": validity,
    }
