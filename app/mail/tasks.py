"""Celery tasks for mail. The beat schedule sweeps the outbox every minute, so mail is
only delayed, never lost, if the broker was unavailable when a message was queued."""

from app.core.worker import celery_app
from app.mail import service


@celery_app.task(name="mail.send_pending")  # type: ignore[untyped-decorator]
def send_pending() -> int:
    return service.send_pending()


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "mail-sweep": {"task": "mail.send_pending", "schedule": 60.0},
}
