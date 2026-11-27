"""Shared fleet helpers (ADR 0033): permission keys, labels, settings, the odometer and
document status, and who drives what."""

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.authz import engine
from app.core import clock
from app.core.errors import NotFound, PermissionDenied
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.fleet.models import (
    Booking,
    Driver,
    FleetSettings,
    FuelLog,
    Maintenance,
    Vehicle,
    VehicleDocument,
)
from app.modules.hr import service as people

READ = "fleet.vehicle.read"
MANAGE = "fleet.vehicle.manage"
COSTS = "fleet.cost.read"
USE = "fleet.vehicle.use"
ZERO = Decimal(0)
TYPE_LABELS = {
    "car": "Car",
    "pickup": "Pickup",
    "van": "Van",
    "bus": "Bus",
    "truck": "Truck",
    "motorcycle": "Motorcycle",
}
FUEL_LABELS = {"petrol": "Petrol", "diesel": "Diesel", "cng": "CNG", "electric": "Electric"}
STATUS_LABELS = {"active": "Active", "workshop": "In the workshop", "retired": "Retired"}
STATUS_TONES = {"active": "success", "workshop": "warning", "retired": "neutral"}
DOCUMENT_LABELS = {
    "insurance": "Insurance",
    "licence": "Vehicle licence",
    "roadworthiness": "Roadworthiness",
    "other": "Other",
}


def can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def require(db: Session, actor: Actor, permission: str) -> None:
    if not can(db, actor, permission):
        raise PermissionDenied()


def uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def parse_decimal(
    text: str, key: str, errors: dict[str, str], *, label: str, places: str = "0.01"
) -> Decimal | None:
    raw = (text or "").strip().replace(",", "").replace("₦", "")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = Decimal(-1)
    if not value.is_finite() or value < 0 or value != value.quantize(Decimal(places)):
        errors[key] = f"Enter {label}."
        return None
    return value


def parse_int(text: str, key: str, errors: dict[str, str], *, label: str) -> int | None:
    raw = (text or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        errors[key] = f"Enter {label}."
        return None
    return value


def me(db: Session, actor: Actor) -> uuid.UUID | None:
    return people.employee_of_user(db, actor.user_id)


def user_names(db: Session, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all())


def settings(db: Session) -> FleetSettings:
    row = db.get(FleetSettings, 1)
    if row is None:  # seeded by the migration; recreated if it ever goes missing
        row = FleetSettings(id=1, first_alert_days=30, second_alert_days=7, low_efficiency_pct=70)
        db.add(row)
        db.flush()
    return row


def vehicle_of(db: Session, vehicle_id: uuid.UUID, *, lock: bool = False) -> Vehicle:
    query = select(Vehicle).where(Vehicle.id == vehicle_id)
    row = db.scalars(query.with_for_update() if lock else query).first()
    if row is None:
        raise NotFound()
    return row


def label(vehicle: Vehicle) -> str:
    return f"{vehicle.registration} · {vehicle.make} {vehicle.model}"


def odometer(db: Session, vehicle: Vehicle) -> int:
    """The highest reading recorded: fuel logs, services, or the starting odometer."""
    fuel = db.scalar(select(func.max(FuelLog.odometer)).where(FuelLog.vehicle_id == vehicle.id))
    service = db.scalar(
        select(func.max(Maintenance.odometer)).where(Maintenance.vehicle_id == vehicle.id)
    )
    return max(vehicle.start_odometer, fuel or 0, service or 0)


def current_documents(db: Session, vehicle_id: uuid.UUID) -> dict[str, VehicleDocument]:
    """The latest-expiring document of each kind."""
    out: dict[str, VehicleDocument] = {}
    for doc in db.scalars(
        select(VehicleDocument)
        .where(VehicleDocument.vehicle_id == vehicle_id)
        .order_by(VehicleDocument.expires_on)
    ):
        out[doc.kind] = doc
    return out


def expired_documents(db: Session, vehicle_id: uuid.UUID, on: date) -> list[str]:
    return [
        DOCUMENT_LABELS[kind]
        for kind, doc in current_documents(db, vehicle_id).items()
        if doc.expires_on < on
    ]


def licensed(db: Session, employee_id: uuid.UUID | None, on: date) -> Driver | None:
    """The person's driver record if it's active and their licence is in date."""
    if employee_id is None:
        return None
    driver = db.scalars(select(Driver).where(Driver.employee_id == employee_id)).first()
    if driver is None or not driver.is_active or driver.licence_expiry < on:
        return None
    return driver


def drives(db: Session, employee_id: uuid.UUID | None, vehicle: Vehicle, at: datetime) -> bool:
    """The assigned driver, or the requester or driver of an approved booking that day."""
    if employee_id is None:
        return False
    if vehicle.driver_employee_id == employee_id:
        return True
    day = clock.local_date(at)
    for booking in db.scalars(
        select(Booking).where(Booking.vehicle_id == vehicle.id, Booking.status == "approved")
    ):
        if employee_id in (booking.employee_id, booking.driver_employee_id) and (
            clock.local_date(booking.starts_at) <= day <= clock.local_date(booking.ends_at)
        ):
            return True
    return False
