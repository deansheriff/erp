"""Cost per vehicle (ADR 0033): ledger costs naming each vehicle (by account), with
distance and litres from fuel logs, cost per kilometre and kilometres per litre. Also
the fleet settings."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core.errors import ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.fleet import common
from app.modules.fleet.models import FuelLog, Vehicle

ZERO = Decimal(0)


@dataclass(slots=True)
class CostRow:
    vehicle: Vehicle
    accounts: dict[str, Decimal] = field(default_factory=dict)
    distance: int = 0
    litres: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return sum(self.accounts.values(), ZERO)

    @property
    def per_km(self) -> Decimal | None:
        if not self.distance:
            return None
        return (self.total / self.distance).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def km_per_litre(self) -> Decimal | None:
        if not self.litres:
            return None
        return (Decimal(self.distance) / self.litres).quantize(Decimal("0.1"))


def cost_rows(
    db: Session, actor: Actor, *, start: date | None, end: date | None
) -> tuple[list[CostRow], list[str]]:
    """Rows by vehicle, and the accounts that appear (for the columns)."""
    common.require(db, actor, common.COSTS)
    vehicles = list(db.scalars(select(Vehicle).order_by(Vehicle.code)))
    rows = {v.id: CostRow(v) for v in vehicles}
    accounts: set[str] = set()
    for cost in finance.vehicle_costs(db, set(rows), start=start, end=end):
        rows[cost.vehicle_id].accounts[cost.account] = cost.net
        accounts.add(cost.account)
    query = select(
        FuelLog.vehicle_id,
        func.min(FuelLog.odometer),
        func.max(FuelLog.odometer),
        func.sum(FuelLog.litres),
    ).group_by(FuelLog.vehicle_id)
    if start:
        query = query.where(FuelLog.day >= start)
    if end:
        query = query.where(FuelLog.day <= end)
    fuel: dict[uuid.UUID, tuple[int, int, Decimal]] = defaultdict(lambda: (0, 0, ZERO))
    for vehicle_id, low, high, litres in db.execute(query).all():
        fuel[vehicle_id] = (low or 0, high or 0, litres or ZERO)
    for vehicle_id, row in rows.items():
        low, high, litres = fuel[vehicle_id]
        row.distance, row.litres = max(high - low, 0), litres
    shown = [r for r in rows.values() if r.total or r.litres or r.vehicle.status != "retired"]
    return shown, sorted(accounts)


def save_settings(db: Session, actor: Actor, *, first: str, second: str, efficiency: str) -> None:
    common.require(db, actor, common.MANAGE)
    errors: dict[str, str] = {}
    values = {}
    for key, text, low, high in (
        ("first_alert_days", first, 1, 365),
        ("second_alert_days", second, 1, 365),
        ("low_efficiency_pct", efficiency, 1, 99),
    ):
        try:
            values[key] = int(text.strip())
        except ValueError:
            values[key] = 0
        if not low <= values[key] <= high:
            errors[key] = f"Enter a number from {low} to {high}."
    if not errors and values["second_alert_days"] >= values["first_alert_days"]:
        errors["second_alert_days"] = "The second alert must come later (fewer days before)."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = common.settings(db)
    row.first_alert_days = values["first_alert_days"]
    row.second_alert_days = values["second_alert_days"]
    row.low_efficiency_pct = values["low_efficiency_pct"]
    db.flush()
    record_by(
        db,
        actor,
        "fleet.settings.update",
        entity_type="fleet_settings",
        entity_id="1",
        changes=values,
    )
