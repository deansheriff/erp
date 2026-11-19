"""Celery tasks for leave: write the year's carry-over (ADR 0018). Runs daily at 01:00
UTC (02:00 in Lagos) and only writes what is missing, so 1 January is covered even if
the worker was down that night."""

from celery.schedules import crontab

from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.leave import settings


@celery_app.task(name="leave.carry_over")  # type: ignore[untyped-decorator]
def carry_over() -> int:
    with session_scope() as db:
        return settings.carry_over(db)


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "leave-carry-over": {"task": "leave.carry_over", "schedule": crontab(hour=1, minute=0)},
}
