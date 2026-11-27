"""The AI assistant (ADR 0035): its settings and opt-in, the policy library (versions and
searchable passages), and each request with its checked reply."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

PROVIDERS = ("anthropic", "openai", "gemini")
# What may leave the system, once accepted (ADR 0035).
KINDS = ("policies", "directory", "work_records", "support_tickets")
FEATURES = ("policy", "hiring", "evidence", "ticket")


class AssistantSettings(Base):
    """One row (id 1). Off until a Super Administrator opts in."""

    __tablename__ = "assistant_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="one_row"),
        CheckConstraint("provider IN ('anthropic', 'openai', 'gemini')", name="provider"),
        CheckConstraint("monthly_cap BETWEEN 1 AND 100000", name="cap"),
        CheckConstraint("history_days BETWEEN 1 AND 3650", name="history"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=false())
    provider: Mapped[str] = mapped_column(String(10), server_default="anthropic")
    model: Mapped[str] = mapped_column(String(80), server_default="claude-opus-5-5")
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    api_key_hint: Mapped[str | None] = mapped_column(String(8))  # the last 4 characters
    kinds: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    # The acceptance in force: who accepted which kinds going to which provider.
    accepted_provider: Mapped[str | None] = mapped_column(String(10))
    accepted_kinds: Mapped[list[str] | None] = mapped_column(JSONB)
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    accepted_at: Mapped[datetime | None]
    monthly_cap: Mapped[int] = mapped_column(Integer, server_default="200")
    history_days: Mapped[int] = mapped_column(Integer, server_default="90")


class Policy(IdMixin, Base):
    __tablename__ = "assistant_policies"

    title: Mapped[str] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PolicyVersion(IdMixin, Base):
    """One uploaded version. Only the current one of each policy is searched."""

    __tablename__ = "assistant_policy_versions"
    __table_args__ = (Index("ix_assistant_policy_versions_policy_id", "policy_id"),)

    policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistant_policies.id", ondelete="CASCADE")
    )
    version: Mapped[str] = mapped_column(String(20))
    effective_on: Mapped[date]
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=false())
    passages: Mapped[int] = mapped_column(Integer, server_default="0")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Passage(IdMixin, Base):
    __tablename__ = "assistant_policy_passages"
    __table_args__ = (
        Index("ix_assistant_policy_passages_version_id", "version_id"),
        Index("ix_assistant_policy_passages_search", "search", postgresql_using="gin"),
    )

    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistant_policy_versions.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    search: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', body)", persisted=True)
    )


class AssistantRequest(IdMixin, Base):
    """One request: what was asked, the checked reply, and what it cost. Seen only by its
    owner; deleted after the history period."""

    __tablename__ = "assistant_requests"
    __table_args__ = (
        CheckConstraint("feature IN ('policy', 'hiring', 'evidence', 'ticket')", name="feature"),
        CheckConstraint("status IN ('ok', 'unverified', 'refused', 'failed')", name="status"),
        Index("ix_assistant_requests_user_id_created_at", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    feature: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(200))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    reply: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    status: Mapped[str] = mapped_column(String(10))
    provider: Mapped[str] = mapped_column(String(10))
    model: Mapped[str] = mapped_column(String(80))
    kinds_sent: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
