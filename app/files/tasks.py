"""Celery tasks for files: scan pending uploads every 30 seconds, and apply retention
nightly at 02:00 UTC (03:00 in Lagos)."""

from celery.schedules import crontab

from app.core.worker import celery_app
from app.files import service


@celery_app.task(name="files.scan_pending")  # type: ignore[untyped-decorator]
def scan_pending() -> int:
    return service.scan_pending()


@celery_app.task(name="files.run_retention")  # type: ignore[untyped-decorator]
def run_retention() -> int:
    return service.run_retention()


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "files-scan": {"task": "files.scan_pending", "schedule": 30.0},
    "files-retention": {"task": "files.run_retention", "schedule": crontab(hour=2, minute=0)},
}
