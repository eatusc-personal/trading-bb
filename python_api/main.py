"""FastAPI application entry point (`uvicorn python_api.main:app`).

Creates the app, installs the CORS and rate-limit middleware, runs the SQLite
retention pass at startup, and includes one router per API area. Endpoint code
lives in python_api/routers/, and the logic behind it in the service modules
next to this file.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=os.getenv("API_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("openbb_api")


from .config import (
    API_RATE_LIMIT_MAX_REQUESTS,
    API_RATE_LIMIT_WINDOW_SEC,
    APP_NAME,
    CORS_ALLOW_ORIGINS,
    SOCIAL_RATE_LIMIT_MAX_REQUESTS,
    SOCIAL_RATE_LIMIT_WINDOW_SEC,
)
from .retention import _schedule_daily_prune, prune_sqlite_caches
from .routers import (
    alerts as alerts_routes,
    etfs as etfs_routes,
    market as market_routes,
    screener as screener_routes,
    social as social_routes,
    symbol_master as symbol_master_routes,
    system as system_routes,
    ticker as ticker_routes,
    watchlist as watchlist_routes,
)
from .security import client_ip, rate_limit_check

# Re-exported for modules that read these off python_api.main through lazy
# imports (report.py, divergence.py). Keep them importable from here.
from .config import MARKET_DATA_PROVIDER, SCREENER_HISTORY_WORKERS, SYMBOL_PATTERN  # noqa: F401
from .after_hours import _build_market_after_hours  # noqa: F401
from .etfs import get_popular_etf_snapshot  # noqa: F401
from .market_data import fetch_history, fetch_quote, validate_symbol  # noqa: F401
from .market_research import _build_market_overview  # noqa: F401
from .news import _build_news_headlines  # noqa: F401
from .routers.alerts import _build_alerts  # noqa: F401
from .routers.market import _build_market_indicators  # noqa: F401
from .routers.watchlist import watchlist_quote  # noqa: F401
from .social_refresh import _social_result_lock, _social_result_state  # noqa: F401
from .swr import swr_serve  # noqa: F401
from .utils import now_iso  # noqa: F401

app = FastAPI(title=APP_NAME, version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS or ["http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def api_rate_limit_middleware(request: FastAPIRequest, call_next):
    path = str(request.url.path or "")
    if not path.startswith("/api/"):
        return await call_next(request)
    if path in {"/api/health"}:
        return await call_next(request)

    subject = client_ip(request)
    ok_api, api_detail = rate_limit_check(
        "api",
        subject,
        max(10, API_RATE_LIMIT_MAX_REQUESTS),
        max(1, API_RATE_LIMIT_WINDOW_SEC),
    )
    if not ok_api:
        return JSONResponse(
            {"error": "rate_limit_exceeded", "scope": "api", "retry_after_sec": api_detail},
            status_code=429,
            headers={"Retry-After": str(api_detail)},
        )

    if path.startswith("/api/social/"):
        ok_social, social_detail = rate_limit_check(
            "social",
            subject,
            max(5, SOCIAL_RATE_LIMIT_MAX_REQUESTS),
            max(1, SOCIAL_RATE_LIMIT_WINDOW_SEC),
        )
        if not ok_social:
            return JSONResponse(
                {"error": "rate_limit_exceeded", "scope": "social", "retry_after_sec": social_detail},
                status_code=429,
                headers={"Retry-After": str(social_detail)},
            )

    return await call_next(request)


@app.on_event("startup")
def _startup_retention() -> None:
    try:
        prune_sqlite_caches()
    except Exception as exc:
        logger.warning("SQLite retention pass failed at startup: %s", exc)
    _schedule_daily_prune()


# Routers are included in the order their routes were originally declared.
app.include_router(system_routes.router)
app.include_router(symbol_master_routes.router)
app.include_router(market_routes.router)
app.include_router(ticker_routes.router)
app.include_router(screener_routes.router)
app.include_router(social_routes.router)
app.include_router(etfs_routes.router)
app.include_router(watchlist_routes.router)
app.include_router(alerts_routes.router)

# ---------------------------------------------------------------------------
# Divergence scanner (peer-group outlier detection) — see python_api/divergence.py
# ---------------------------------------------------------------------------
from .divergence import router as divergence_router  # noqa: E402

app.include_router(divergence_router)

# ---------------------------------------------------------------------------
# Daily cross-tab report (confluence-ranked opportunities) — python_api/report.py
# ---------------------------------------------------------------------------
from .report import router as report_router  # noqa: E402

app.include_router(report_router)
