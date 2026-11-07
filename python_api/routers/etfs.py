"""Popular ETF endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..config import ETF_POPULAR_LIMIT
from ..etfs import get_popular_etf_snapshot

router = APIRouter()


@router.get("/api/etfs/popular")
def popular_etfs(
    limit: int = Query(default=ETF_POPULAR_LIMIT, ge=1, le=500),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    return get_popular_etf_snapshot(limit=limit, refresh=refresh)
