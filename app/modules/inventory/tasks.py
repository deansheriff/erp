"""Inventory background tasks: the daily low-stock digest (ADR 0028)."""

from celery.schedules import crontab

from app.core import clock
from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.finance import service as finance
from app.modules.inventory import alerts


@celery_app.task(name="inventory.low_stock_digest")  # type: ignore[untyped-decorator]
def low_stock_digest() -> int:
    with session_scope() as db:
        return alerts.send_digest(db, clock.today(), finance.organisation_name(db))


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "inventory-low-stock": {
        "task": "inventory.low_stock_digest",
        "schedule": crontab(hour=7, minute=0),  # 08:00 in Lagos
    },
}
