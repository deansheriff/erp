"""Chat background tasks (ADR 0034): department channel membership every 10 minutes,
unread emails every 5 minutes, and the daily retention purge (when one is set)."""

from celery.schedules import crontab

from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.chat import channels, notices
from app.modules.finance import service as finance


@celery_app.task(name="chat.sync_channels")  # type: ignore[untyped-decorator]
def sync_channels() -> int:
    with session_scope() as db:
        result = channels.sync(db)
        return result.added + result.removed


@celery_app.task(name="chat.unread_emails")  # type: ignore[untyped-decorator]
def unread_emails() -> int:
    with session_scope() as db:
        return notices.send_unread_emails(db, finance.organisation_name(db))


@celery_app.task(name="chat.retention")  # type: ignore[untyped-decorator]
def retention() -> int:
    with session_scope() as db:
        return notices.purge_old(db)


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "chat-sync-channels": {"task": "chat.sync_channels", "schedule": crontab(minute="*/10")},
    "chat-unread-emails": {"task": "chat.unread_emails", "schedule": crontab(minute="*/5")},
    "chat-retention": {"task": "chat.retention", "schedule": crontab(hour=1, minute=15)},
}
