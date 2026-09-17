"""FastAPI application.

Research tool. No brokerage integration, no order placement, nothing autonomous.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import routes_admin, routes_backtest, routes_feed, routes_user
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "starting %s (env=%s, market=%s, llm=%s)",
        settings.app_name,
        settings.environment,
        settings.market_data_provider,
        "canned" if settings.llm_fake_mode else ("live" if settings.llm_enabled else "disabled"),
    )
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "Research and information tool. Monitors public announcements and shows how "
        "comparable past events related to market movements. Not investment advice."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,  # bearer token in a header; no cookies, so no CSRF surface
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(routes_admin.router)
app.include_router(routes_backtest.router)
app.include_router(routes_feed.router)
app.include_router(routes_user.router)


@app.get("/")
def root() -> dict:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "disclaimer": (
            "Research and information tool. Statistical association between past "
            "events and past price moves is not causation and not a forecast."
        ),
    }
