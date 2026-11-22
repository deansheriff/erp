"""Finance background tasks (ADR 0023): draft the recurring journals that fall due."""

from celery.schedules import crontab

from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.finance import recurring


@celery_app.task(name="finance.draft_recurring")  # type: ignore[untyped-decorator]
def draft_recurring() -> int:
    with session_scope() as db:
        return recurring.generate_due(db)


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "finance-recurring": {
        "task": "finance.draft_recurring",
        "schedule": crontab(hour=1, minute=30),
    },
}
