"""Writing audit events.

`record()` writes inside the caller's transaction, so the event commits or rolls back
with the change it describes. `record_independent()` writes in its own transaction, for
events that must survive a rollback (denied permissions, failed logins).
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.core.context import current_request
from app.core.db import session_scope

MASK = "••••"

ActorType = Literal["user", "system", "anonymous"]
Outcome = Literal["success", "denied", "failure"]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    action: str
    actor_type: ActorType
    outcome: Outcome = "success"
    actor_id: uuid.UUID | None = None
    actor_label: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    changes: dict[str, Any] | None = None
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def diff_changes(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    masked_fields: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Changed fields only, as {field: {"from": old, "to": new}}.

    Fields in `masked_fields` (banking, statutory, compensation and sensitive classes)
    record that they changed, never their values.
    """
    masked = set(masked_fields)
    result: dict[str, dict[str, Any]] = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if key in masked:
            result[key] = {"from": MASK, "to": MASK}
        else:
            result[key] = {"from": _jsonable(old), "to": _jsonable(new)}
    return result


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _to_row(event: AuditEvent) -> AuditLog:
    ctx = current_request()
    return AuditLog(
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        actor_label=event.actor_label,
        action=event.action,
        outcome=event.outcome,
        entity_type=event.entity_type,
        entity_id=event.entity_id,
        changes=event.changes,
        reason=event.reason,
        ip=ctx.ip if ctx else None,
        user_agent=ctx.user_agent[:400] if ctx and ctx.user_agent else None,
        request_id=ctx.request_id if ctx else None,
    )


def record(session: Session, event: AuditEvent) -> None:
    session.add(_to_row(event))


def record_independent(event: AuditEvent) -> None:
    with session_scope() as session:
        session.add(_to_row(event))
