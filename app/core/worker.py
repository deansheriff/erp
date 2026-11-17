"""Celery application. Run with: celery -A app.core.worker worker

Tasks are plain functions that call service functions inside `session_scope()`.
"""

from celery import Celery

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

celery_app = Celery("kxerp", broker=settings.redis_url, backend=None)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)
celery_app.autodiscover_tasks(["app"], related_name="tasks")
