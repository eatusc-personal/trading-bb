"""Watchlist quote endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..config import MARKET_DATA_PROVIDER
from ..market_data import fetch_quote, validate_symbol
from ..utils import finite_or, now_iso

router = APIRouter()


@router.get("/api/watchlist/quote")
def watchlist_quote(symbols: str = Query(..., description="Comma-separated symbols")) -> dict[str, Any]:
    parsed = [validate_symbol(s) for s in symbols.split(",") if s.strip()]
    if not parsed:
        raise HTTPException(status_code=400, detail="No valid symbols provided")

    df = fetch_quote(parsed, enrich=False)
    rows = []
    for _, r in df.iterrows():
        symbol = str(r.get("symbol") or "").upper()
        prev_close = finite_or(r.get("prev_close"), 0)
        last_price = finite_or(r.get("last_price"), 0)
        change_pct = ((last_price - prev_close) / prev_close * 100) if prev_close else None
        rows.append(
            {
                "symbol": symbol,
                "name": str(r.get("name") or symbol),
                "price": finite_or(last_price, 0),
                "change_pct": change_pct,
                "volume": finite_or(r.get("volume"), 0),
                "provider": str(r.get("source_provider") or ""),
            }
        )

    return {
        "quotes": rows,
        "count": len(rows),
        "updated_at": now_iso(),
        "provider": MARKET_DATA_PROVIDER,
        "source_providers": df.attrs.get("source_providers", []),
    }
