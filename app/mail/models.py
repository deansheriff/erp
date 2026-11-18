"""SMTP settings and the outbox (ADR 0012)."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin


class MailSettings(IdMixin, Base):
    __tablename__ = "mail_settings"
    __table_args__ = (
        CheckConstraint("is_singleton", name="one_row"),
        CheckConstraint("security IN ('starttls', 'ssl', 'none')", name="security"),
    )

    is_singleton: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), unique=True
    )
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    security: Mapped[str] = mapped_column(String(10))
    username: Mapped[str | None] = mapped_column(String(255))
    password_encrypted: Mapped[str | None] = mapped_column(Text)
    from_name: Mapped[str] = mapped_column(String(200))
    from_address: Mapped[str] = mapped_column(String(254))
    reply_to: Mapped[str | None] = mapped_column(String(254))
    last_test_at: Mapped[datetime | None]
    last_test_ok: Mapped[bool | None]
    last_test_to: Mapped[str | None] = mapped_column(String(254))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class MailOutbox(IdMixin, Base):
    __tablename__ = "mail_outbox"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'sent', 'failed')", name="status"),
        Index("ix_mail_outbox_status_next", "status", "next_attempt_at"),
    )

    to_address: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text)
    template: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(String(40))  # a category, never raw text
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]
