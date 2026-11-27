"""Fuel logs (ADR 0033). The vehicle's driver (assigned, or on an approved booking that
day) or a Fleet Manager logs fuel; the odometer can't go backwards; a fill whose
kilometres per litre is below the set share of the vehicle's average is flagged."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.files import service as files
from app.identity.schemas import Actor
from app.modules.fleet import common
from app.modules.fleet.models import FuelLog, Vehicle
from app.modules.hr import service as people


@dataclass(frozen=True, slots=True)
class FuelInput:
    vehicle_id: str
    day: date | None
    litres: str
    amount: str
    odometer: str
    station: str
    reference: str


def _previous(db: Session, vehicle_id: uuid.UUID) -> FuelLog | None:
    return db.scalars(
        select(FuelLog)
        .where(FuelLog.vehicle_id == vehicle_id)
        .order_by(FuelLog.odometer.desc())
        .limit(1)
    ).first()


def _average(db: Session, vehicle_id: uuid.UUID) -> Decimal | None:
    rates = [
        r
        for r in db.scalars(
            select(FuelLog.km_per_litre).where(
                FuelLog.vehicle_id == vehicle_id, FuelLog.km_per_litre.is_not(None)
            )
        )
        if r
    ]
    return sum(rates, Decimal(0)) / len(rates) if rates else None


def log(
    db: Session,
    actor: Actor,
    data: FuelInput,
    *,
    filename: str | None = None,
    mime: str | None = None,
    receipt: bytes = b"",
) -> uuid.UUID:
    common.require(db, actor, common.USE)
    vehicle = db.get(Vehicle, common.uuid_or_none(data.vehicle_id) or uuid.uuid4())
    errors: dict[str, str] = {}
    if vehicle is None or vehicle.status == "retired":
        raise ValidationFailed(field_errors={"vehicle_id": "Choose a vehicle in use."})
    me = common.me(db, actor)
    if not (common.can(db, actor, common.MANAGE) or common.drives(db, me, vehicle, clock.now())):
        raise PermissionDenied("Fuel is logged by the vehicle's driver or a Fleet Manager.")
    if data.day is None:
        errors["fuel_day"] = "Enter the date."
    elif data.day > clock.today():
        errors["fuel_day"] = "The date can't be in the future."
    litres = common.parse_decimal(data.litres, "litres", errors, label="litres, like 45.5")
    amount = common.parse_decimal(data.amount, "fuel_amount", errors, label="the amount paid")
    reading = common.parse_int(data.odometer, "fuel_odometer", errors, label="the odometer in km")
    if litres is not None and litres <= 0:
        errors["litres"] = "Enter litres, like 45.5."
    for key, value in (("litres", litres), ("fuel_amount", amount), ("fuel_odometer", reading)):
        if value is None and key not in errors:
            errors[key] = "This is needed."
    last = common.odometer(db, vehicle)
    if reading is not None and reading < last:
        errors["fuel_odometer"] = f"The odometer can't go back: it last read {last:,} km."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert litres is not None and amount is not None and reading is not None  # noqa: S101
    previous = _previous(db, vehicle.id)
    rate = None
    if previous is not None and reading > previous.odometer:
        rate = (Decimal(reading - previous.odometer) / litres).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    average = _average(db, vehicle.id)
    flagged = bool(
        rate is not None
        and average
        and rate < average * common.settings(db).low_efficiency_pct / 100
    )
    row = FuelLog(
        vehicle_id=vehicle.id,
        employee_id=me,
        day=data.day,
        litres=litres,
        amount=amount,
        odometer=reading,
        station=data.station.strip()[:120] or None,
        reference=data.reference.strip()[:40] or None,
        km_per_litre=rate,
        flagged=flagged,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    if receipt:
        row.receipt_file_id = files.upload(
            db,
            actor,
            purpose_key="fleet_receipt",
            entity_id=str(row.id),
            filename=filename,
            declared_mime=mime,
            data=receipt,
        )
    db.flush()
    record_by(
        db,
        actor,
        "fleet.fuel.log",
        entity_type="vehicle",
        entity_id=str(vehicle.id),
        changes={"litres": str(litres), "odometer": reading, "flagged": flagged},
    )
    return row.id


def my_vehicles(db: Session, actor: Actor) -> list[Vehicle]:
    """Vehicles the person can log fuel on today: assigned to them, or booked."""
    me = common.me(db, actor)
    if me is None:
        return []
    now = clock.now()
    return [
        v
        for v in db.scalars(
            select(Vehicle).where(Vehicle.status != "retired").order_by(Vehicle.registration)
        )
        if common.drives(db, me, v, now)
    ]


@dataclass(frozen=True, slots=True)
class LogRow:
    log: FuelLog
    vehicle: str
    driver: str | None


def recent(db: Session, actor: Actor, *, limit: int = 20) -> list[LogRow]:
    """The person's own recent logs (all of them for Fleet Managers)."""
    query = select(FuelLog).order_by(FuelLog.day.desc(), FuelLog.created_at.desc()).limit(limit)
    if not common.can(db, actor, common.READ):
        me = common.me(db, actor)
        if me is None:
            return []
        query = query.where(FuelLog.employee_id == me)
    logs = list(db.scalars(query))
    vehicles = {
        v.id: v
        for v in db.scalars(select(Vehicle).where(Vehicle.id.in_({x.vehicle_id for x in logs})))
    }
    names = people.names(db, {x.employee_id for x in logs if x.employee_id})
    return [
        LogRow(
            x,
            common.label(vehicles[x.vehicle_id]),
            names.get(x.employee_id) if x.employee_id else None,
        )
        for x in logs
    ]


def ensure_open(vehicle: Vehicle) -> None:
    if vehicle.status == "retired":
        raise Conflict("This vehicle is retired.")
