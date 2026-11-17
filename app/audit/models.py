"""The append-only audit log (ADR 0007). kx_app may INSERT and SELECT, nothing else."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Text, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_occurred_at", "occurred_at"),
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_actor_id", "actor_id"),
    )

    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor_type: Mapped[str] = mapped_column(String(20))  # user | system | anonymous
    actor_id: Mapped[uuid.UUID | None]
    actor_label: Mapped[str | None] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(120))  # e.g. "expense.claim.approve"
    outcome: Mapped[str] = mapped_column(String(20))  # success | denied | failure
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(400))
    request_id: Mapped[uuid.UUID | None]
