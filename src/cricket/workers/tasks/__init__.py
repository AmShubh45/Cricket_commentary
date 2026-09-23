"""Celery tasks for the cricket commentary pipeline.

These tasks are the async work units that Celery workers execute:
- poll_match: Fetch latest ball data and process through the pipeline
- generate_tts: Convert commentary text to speech audio
- heartbeat: Monitor pipeline health and alert on failures
"""

from __future__ import annotations

import asyncio
import logging

from cricket.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="cricket.poll_match",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
)
def poll_match(self, match_id: str) -> dict:  # type: ignore[no-untyped-def]
    """Poll a live match for new ball data and process through the pipeline.

    This task is called by Celery Beat on a schedule (every N seconds).
    It's the Celery-based alternative to the async polling loop in
    PipelineOrchestrator.start().
    """
    from cricket.infra.factory import ProviderFactory

    try:
        factory = ProviderFactory()
        pipeline = factory.create_pipeline()

        # Run the async poll-and-process in a sync context (Celery is sync)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(pipeline._poll_and_process(match_id))
        finally:
            loop.close()

        return {"status": "ok", "match_id": match_id}

    except Exception as exc:
        logger.exception("Poll task failed for match %s", match_id)
        raise self.retry(exc=exc)


@celery_app.task(name="cricket.heartbeat")
def heartbeat() -> dict:
    """Periodic heartbeat task to verify the pipeline is alive.

    If this task fails to execute, Celery Beat monitoring will detect
    the gap and trigger an alert.
    """
    from cricket.infra.factory import ProviderFactory

    factory = ProviderFactory()
    state_store = factory.create_state_store()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        redis_healthy = loop.run_until_complete(state_store.healthcheck())
    finally:
        loop.close()

    status = {
        "status": "alive",
        "redis": redis_healthy,
    }

    if not redis_healthy:
        logger.error("HEARTBEAT ALERT: Redis is down!")

    return status


@celery_app.task(name="cricket.cleanup_match")
def cleanup_match(match_id: str) -> dict:
    """Clean up Redis state and temporary files after a match ends."""
    from cricket.infra.factory import ProviderFactory

    factory = ProviderFactory()
    state_store = factory.create_state_store()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(state_store.delete_match_data(match_id))
    finally:
        loop.close()

    logger.info("Cleaned up match data for %s", match_id)
    return {"status": "cleaned", "match_id": match_id}
