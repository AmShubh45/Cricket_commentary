"""Health check routes — for K8s probes and monitoring."""

from __future__ import annotations

from fastapi import APIRouter

from cricket.infra.factory import ProviderFactory

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Basic liveness probe — always returns 200 if the process is running."""
    return {"status": "ok", "service": "cricket-commentary"}


@router.get("/health/ready")
async def readiness() -> dict:
    """Readiness probe — checks all external dependencies."""
    factory = ProviderFactory()

    checks = {}

    # Redis
    try:
        state_store = factory.create_state_store()
        checks["redis"] = await state_store.healthcheck()
    except Exception:
        checks["redis"] = False

    # Data feed
    try:
        data_feed = factory.create_data_feed()
        checks["data_feed"] = await data_feed.healthcheck()
    except Exception:
        checks["data_feed"] = False

    # TTS
    try:
        tts = factory.create_tts()
        checks["tts"] = await tts.healthcheck()
    except Exception:
        checks["tts"] = False

    all_healthy = all(checks.values())
    return {
        "status": "ready" if all_healthy else "degraded",
        "checks": checks,
    }
