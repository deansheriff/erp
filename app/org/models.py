"""The organisation this installation serves (ADR 0002). Exactly one row."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, SmallInteger, String, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin


class Organization(IdMixin, Base):
    __tablename__ = "organization"
    __table_args__ = (
        CheckConstraint("is_singleton", name="one_row"),
        CheckConstraint("fiscal_year_start_month BETWEEN 1 AND 12", name="fiscal_month"),
    )

    # Always true and unique, so a second row can never be inserted.
    is_singleton: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), unique=True
    )
    legal_name: Mapped[str] = mapped_column(String(200))
    trading_name: Mapped[str | None] = mapped_column(String(200))
    rc_number: Mapped[str | None] = mapped_column(String(30))
    tin: Mapped[str | None] = mapped_column(String(30))
    address: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), server_default="Africa/Lagos")
    base_currency: Mapped[str] = mapped_column(String(3), server_default="NGN")
    fiscal_year_start_month: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
