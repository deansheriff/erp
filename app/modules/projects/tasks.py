"""Projects background tasks: the daily budget alerts (ADR 0031)."""

from celery.schedules import crontab

from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.finance import service as finance
from app.modules.projects import figures


@celery_app.task(name="projects.budget_alerts")  # type: ignore[untyped-decorator]
def budget_alerts() -> int:
    with session_scope() as db:
        return figures.send_budget_alerts(db, finance.organisation_name(db))


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "projects-budget-alerts": {
        "task": "projects.budget_alerts",
        "schedule": crontab(hour=7, minute=15),  # 08:15 in Lagos
    },
}
