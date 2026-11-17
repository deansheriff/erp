"""The audit log is append-only for the application (ADR 0007)."""

import uuid

import pytest
from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.audit.service import AuditEvent, record, record_independent
from app.core.context import RequestContext, reset_request_context, set_request_context

pytestmark = pytest.mark.db


def _event(action: str = "test.event.create", **kwargs: object) -> AuditEvent:
    return AuditEvent(action=action, actor_type="system", **kwargs)  # type: ignore[arg-type]


def test_record_writes_in_the_callers_transaction(db: Session) -> None:
    record(db, _event(entity_type="test", entity_id="1", changes={"a": {"from": 1, "to": 2}}))
    db.flush()
    row = db.scalars(select(AuditLog).where(AuditLog.action == "test.event.create")).one()
    assert row.changes == {"a": {"from": 1, "to": 2}}
    assert row.outcome == "success"
    assert row.occurred_at is not None


def test_request_context_is_captured(db: Session) -> None:
    rid = uuid.uuid4()
    token = set_request_context(RequestContext(request_id=rid, ip="102.89.1.20", user_agent="UA"))
    try:
        record(db, _event("test.context"))
        db.flush()
    finally:
        reset_request_context(token)
    row = db.scalars(select(AuditLog).where(AuditLog.action == "test.context")).one()
    assert row.request_id == rid
    assert str(row.ip) == "102.89.1.20"


def test_app_role_cannot_update_audit_rows(db: Session) -> None:
    record(db, _event("test.update"))
    db.flush()
    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(update(AuditLog).values(reason="tampered"))
        db.flush()


def test_app_role_cannot_delete_audit_rows(db: Session) -> None:
    record(db, _event("test.delete"))
    db.flush()
    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(delete(AuditLog))


def test_app_role_cannot_truncate_the_audit_log(db: Session) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(text("TRUNCATE audit_log"))


def test_independent_records_survive_a_rolled_back_request(engine: Engine) -> None:
    action = f"test.denied.{uuid.uuid4().hex[:8]}"
    with Session(engine) as session, session.begin():
        record_independent(_event(action, outcome="denied", reason="no permission"))
        session.rollback()
    with engine.connect() as conn:
        count = conn.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
        ).scalar_one()
    assert count == 1
