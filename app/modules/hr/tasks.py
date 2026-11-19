"""Celery tasks for HR: revoke access for leavers every 15 minutes (ADR 0017), so access
ends shortly after midnight Lagos time following the last working day."""

from app.core.db import session_scope
from app.core.worker import celery_app
from app.modules.hr import exits


@celery_app.task(name="hr.revoke_due_access")  # type: ignore[untyped-decorator]
def revoke_due_access() -> int:
    with session_scope() as db:
        return exits.revoke_due(db)


celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "hr-revoke-access": {"task": "hr.revoke_due_access", "schedule": 15 * 60.0},
}
