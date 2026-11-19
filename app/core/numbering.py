"""Gap-free document numbers (brief section 8). Each sequence is one row, locked while a
number is taken, so numbers are allocated in the same transaction as the document: a
rolled-back document gives its number back."""

from sqlalchemy import CheckConstraint, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.db import Base


class NumberSequence(Base):
    __tablename__ = "number_sequences"
    __table_args__ = (
        CheckConstraint("next_value >= 1", name="next_value"),
        CheckConstraint("padding BETWEEN 1 AND 12", name="padding"),
    )

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(20), server_default="")
    padding: Mapped[int] = mapped_column(Integer, server_default="5")
    next_value: Mapped[int] = mapped_column(Integer, server_default="1")

    def format(self, value: int) -> str:
        return f"{self.prefix}{value:0{self.padding}d}"


def next_number(db: Session, key: str) -> str:
    """Take the next number of a sequence. Callers must be inside the document's
    transaction."""
    row = db.scalars(
        select(NumberSequence).where(NumberSequence.key == key).with_for_update()
    ).one()
    value = row.next_value
    row.next_value = value + 1
    db.flush()
    return row.format(value)


def preview(db: Session, key: str) -> str:
    row = db.get(NumberSequence, key)
    return row.format(row.next_value) if row else ""


def configure(db: Session, key: str, *, prefix: str, padding: int) -> None:
    row = db.scalars(
        select(NumberSequence).where(NumberSequence.key == key).with_for_update()
    ).one()
    row.prefix, row.padding = prefix, padding
