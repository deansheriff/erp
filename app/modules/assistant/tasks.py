"""Assistant background tasks (ADR 0035): deleting history past its period, nightly."""

from celery.schedules import crontab

from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.assistant import history


@celery_app.task(name="assistant.purge_history")  # type: ignore[untyped-decorator]
def purge_history() -> int:
    with session_scope() as db:
        return history.purge(db)


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "assistant-purge-history": {
        "task": "assistant.purge_history",
        "schedule": crontab(hour=1, minute=40),
    },
}
