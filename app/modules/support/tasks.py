"""Support background tasks: the SLA watch and closing resolved tickets (ADR 0032)."""

from celery.schedules import crontab

from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.support import watch


@celery_app.task(name="support.sla_watch")  # type: ignore[untyped-decorator]
def sla_watch() -> int:
    with session_scope() as db:
        done = watch.run(db)
        return done.warned + done.breached + done.closed


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "support-sla-watch": {"task": "support.sla_watch", "schedule": crontab(minute="*/15")},
}
