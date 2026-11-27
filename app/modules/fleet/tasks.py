"""Fleet background tasks: the daily expiry and service alerts (ADR 0033)."""

from celery.schedules import crontab

from app.core import clock
from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.finance import service as finance
from app.modules.fleet import alerts


@celery_app.task(name="fleet.daily_alerts")  # type: ignore[untyped-decorator]
def daily_alerts() -> int:
    with session_scope() as db:
        sent = alerts.run(db, clock.today(), finance.organisation_name(db))
        return sent.expiries + sent.services


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "fleet-daily-alerts": {
        "task": "fleet.daily_alerts",
        "schedule": crontab(hour=6, minute=30),  # 07:30 in Lagos
    },
}
