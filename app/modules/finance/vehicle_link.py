"""How finance (and the modules posting through it) names vehicles without importing the
fleet module (ADR 0033). Fleet implements `VehicleLink`; `app/wiring.py` connects it with
`set_link` at start-up. Cost per vehicle is read back with `vehicle_costs`."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.finance.models import Account, Journal, JournalLine


class VehicleLink(Protocol):
    def choices(self, db: Session) -> list[tuple[uuid.UUID, str]]:
        """Vehicles that aren't retired, as (id, "code · registration make model")."""
        ...

    def is_open(self, db: Session, vehicle_id: uuid.UUID) -> bool: ...

    def labels(self, db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]: ...


_link: VehicleLink | None = None


def set_link(link: VehicleLink) -> None:
    global _link  # noqa: PLW0603  (set once at start-up by app/wiring.py)
    _link = link


def link() -> VehicleLink:
    if _link is None:
        raise RuntimeError("Fleet isn't connected: call app.wiring.configure_runtime().")
    return _link


def check(db: Session, value: str, key: str, errors: dict[str, str]) -> uuid.UUID | None:
    """A form's vehicle: blank, or a vehicle in use (else an error under `key`)."""
    if not value:
        return None
    try:
        vehicle_id = uuid.UUID(value)
    except ValueError:
        vehicle_id = None
    if vehicle_id is None or not link().is_open(db, vehicle_id):
        errors[key] = "Choose a vehicle in use, or none."
        return None
    return vehicle_id


@dataclass(frozen=True, slots=True)
class VehicleCost:
    vehicle_id: uuid.UUID
    account: str  # "code name"
    net: Decimal  # debit less credit


def vehicle_costs(
    db: Session,
    vehicle_ids: set[uuid.UUID],
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[VehicleCost]:
    """Posted expense lines naming each vehicle, by account."""
    if not vehicle_ids:
        return []
    query = (
        select(
            JournalLine.vehicle_id,
            Account.code,
            Account.name,
            func.sum(JournalLine.debit - JournalLine.credit),
        )
        .join(Journal, Journal.id == JournalLine.journal_id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalLine.vehicle_id.in_(vehicle_ids),
            Journal.status == "posted",
            Account.type == "expense",
        )
        .group_by(JournalLine.vehicle_id, Account.code, Account.name)
        .order_by(Account.code)
    )
    if start:
        query = query.where(Journal.journal_date >= start)
    if end:
        query = query.where(Journal.journal_date <= end)
    return [
        VehicleCost(v, f"{code} {name}", net) for v, code, name, net in db.execute(query).all() if v
    ]
