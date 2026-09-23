"""Celery application configuration.

Sets up the Celery app with Redis as broker and result backend.
Task autodiscovery scans the workers.tasks package.
"""

from __future__ import annotations

from celery import Celery

from config.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "cricket-commentary",
    broker=settings.celery.broker_url,
    backend=settings.celery.result_backend,
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Task execution
    task_acks_late=True,                 # Don't ack until task completes
    worker_prefetch_multiplier=1,        # Fair task distribution
    task_reject_on_worker_lost=True,     # Re-queue tasks if worker crashes

    # Rate limiting
    task_default_rate_limit="10/m",      # Default: 10 tasks per minute

    # Result expiry
    result_expires=3600,                 # Results expire after 1 hour
)

# Autodiscover tasks from the workers.tasks package
celery_app.autodiscover_tasks(["cricket.workers.tasks"])
