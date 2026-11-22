"""Finance background tasks: draft the recurring journals that fall due (ADR 0023), and
email customers the payment reminders due today (ADR 0024)."""

from celery.schedules import crontab

from app.core import clock
from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.finance import customers, recurring


@celery_app.task(name="finance.draft_recurring")  # type: ignore[untyped-decorator]
def draft_recurring() -> int:
    with session_scope() as db:
        return recurring.generate_due(db)


@celery_app.task(name="finance.send_reminders")  # type: ignore[untyped-decorator]
def send_reminders() -> int:
    with session_scope() as db:
        return customers.send_due(db, today=clock.today())


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "finance-recurring": {
        "task": "finance.draft_recurring",
        "schedule": crontab(hour=1, minute=30),
    },
    "finance-reminders": {
        "task": "finance.send_reminders",
        "schedule": crontab(hour=8, minute=0),  # 09:00 in Lagos
    },
}
