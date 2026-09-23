"""FastAPI application — HTTP API for the cricket commentary system.

Provides endpoints for:
- Health checks (for K8s readiness/liveness probes)
- Match management (list live matches, start/stop commentary)
- Stream control (start/stop RTMP stream)
- Cost tracking dashboard
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import get_settings
from cricket.api.routes.costs import router as costs_router
from cricket.api.routes.health import router as health_router
from cricket.api.routes.matches import router as matches_router

# Configure structured logging
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle — startup and shutdown hooks."""
    settings = get_settings()
    logger.info(
        "Starting Cricket Commentary API",
        env=settings.env,
        data_feed=settings.data_feed.provider.value,
        tts=settings.tts.provider.value,
        llm=settings.llm.provider.value,
    )

    yield  # App is running

    logger.info("Shutting down Cricket Commentary API")


app = FastAPI(
    title="Cricket Commentary Engine",
    description="Automated Hindi cricket commentary for YouTube live streaming",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow all origins in development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(health_router, tags=["Health"])
app.include_router(matches_router, prefix="/api/v1", tags=["Matches"])
app.include_router(costs_router, prefix="/api/v1", tags=["Costs"])
