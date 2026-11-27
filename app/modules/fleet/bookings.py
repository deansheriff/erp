"""Vehicle bookings (ADR 0033). Any employee requests a pool vehicle for a time slot; a
Fleet Manager approves it (naming a licensed driver if they like) or rejects it with a
reason. Overlapping approved bookings and vehicles with an expired document are
refused."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.fleet import common
from app.modules.fleet.models import Booking, Vehicle
from app.modules.hr import service as people

STATUS_LABELS = {
    "requested": "Requested",
    "approved": "Approved",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
}
STATUS_TONES = {
    "requested": "warning",
    "approved": "success",
    "rejected": "danger",
    "cancelled": "neutral",
}


def _overlapping(db: Session, row: Booking) -> bool:
    return (
        db.scalar(
            select(Booking.id).where(
                Booking.vehicle_id == row.vehicle_id,
                Booking.status == "approved",
                Booking.id != row.id,
                Booking.starts_at < row.ends_at,
                Booking.ends_at > row.starts_at,
            )
        )
        is not None
    )


def _bookable(db: Session, vehicle: Vehicle | None, starts: datetime) -> str | None:
    if vehicle is None or not vehicle.is_pool or vehicle.status != "active":
        return "Choose a pool vehicle in use."
    expired = common.expired_documents(db, vehicle.id, clock.local_date(starts))
    if expired:
        return f"This vehicle can't be booked: {', '.join(expired)} expired."
    return None


@dataclass(frozen=True, slots=True)
class BookingInput:
    vehicle_id: str
    starts_at: datetime | None
    ends_at: datetime | None
    purpose: str
    destination: str


def request(db: Session, actor: Actor, data: BookingInput) -> uuid.UUID:
    common.require(db, actor, common.USE)
    me = common.me(db, actor)
    if me is None:
        raise PermissionDenied("Bookings are for people on the staff list.")
    errors: dict[str, str] = {}
    vehicle = db.get(Vehicle, common.uuid_or_none(data.vehicle_id) or uuid.uuid4())
    if data.starts_at is None:
        errors["starts_at"] = "Enter when you need it."
    elif data.starts_at < clock.now() - timedelta(minutes=5):
        errors["starts_at"] = "Choose a time from now."
    if data.ends_at is None:
        errors["ends_at"] = "Enter when you'll bring it back."
    elif data.starts_at and data.ends_at <= data.starts_at:
        errors["ends_at"] = "It must end after it starts."
    if not data.purpose.strip():
        errors["purpose"] = "Say what it's for."
    if data.starts_at and (problem := _bookable(db, vehicle, data.starts_at)):
        errors["vehicle_id"] = problem
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert vehicle is not None and data.starts_at and data.ends_at  # noqa: S101
    row = Booking(
        vehicle_id=vehicle.id,
        employee_id=me,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        purpose=data.purpose.strip()[:200],
        destination=data.destination.strip()[:200] or None,
    )
    db.add(row)
    db.flush()
    if _overlapping(db, row):
        raise ValidationFailed(field_errors={"starts_at": "It's already booked then."})
    record_by(db, actor, "fleet.booking.request", entity_type="vehicle", entity_id=str(vehicle.id))
    return row.id


def _row(db: Session, booking_id: uuid.UUID) -> Booking:
    row = db.scalars(select(Booking).where(Booking.id == booking_id).with_for_update()).first()
    if row is None:
        raise NotFound()
    return row


def decide(
    db: Session,
    actor: Actor,
    booking_id: uuid.UUID,
    *,
    approve: bool,
    driver_id: str = "",
    reason: str = "",
) -> None:
    common.require(db, actor, common.MANAGE)
    row = _row(db, booking_id)
    if row.status != "requested":
        raise Conflict("This booking has already been decided.")
    if approve:
        vehicle = db.get(Vehicle, row.vehicle_id)
        problem = _bookable(db, vehicle, row.starts_at)
        if problem:
            raise Conflict(problem)
        if _overlapping(db, row):
            raise Conflict("Another approved booking overlaps this one.")
        driver = common.uuid_or_none(driver_id)
        if driver_id and common.licensed(db, driver, clock.local_date(row.starts_at)) is None:
            raise ValidationFailed(
                field_errors={f"driver_{row.id}": "Choose a driver with a licence in date."}
            )
        row.driver_employee_id = driver
    elif not reason.strip():
        raise ValidationFailed(field_errors={f"reason_{row.id}": "Say why it's rejected."})
    row.status = "approved" if approve else "rejected"
    row.decided_by, row.decided_at = actor.user_id, clock.now()
    row.reason = reason.strip() or None
    db.flush()
    record_by(
        db,
        actor,
        f"fleet.booking.{'approve' if approve else 'reject'}",
        entity_type="vehicle",
        entity_id=str(row.vehicle_id),
    )


def cancel(db: Session, actor: Actor, booking_id: uuid.UUID) -> None:
    row = _row(db, booking_id)
    me = common.me(db, actor)
    if row.employee_id != me and not common.can(db, actor, common.MANAGE):
        raise NotFound()
    if row.status not in ("requested", "approved") or row.ends_at < clock.now():
        raise Conflict("Only an upcoming booking can be cancelled.")
    row.status = "cancelled"
    db.flush()


@dataclass(frozen=True, slots=True)
class BookingRow:
    booking: Booking
    vehicle: str
    requester: str
    driver: str | None


def _rows(db: Session, bookings: list[Booking]) -> list[BookingRow]:
    vehicles = {
        v.id: v
        for v in db.scalars(select(Vehicle).where(Vehicle.id.in_({b.vehicle_id for b in bookings})))
    }
    names = people.names(
        db,
        {b.employee_id for b in bookings}
        | {b.driver_employee_id for b in bookings if b.driver_employee_id},
    )
    return [
        BookingRow(
            b,
            common.label(vehicles[b.vehicle_id]),
            names.get(b.employee_id, ""),
            names.get(b.driver_employee_id) if b.driver_employee_id else None,
        )
        for b in bookings
    ]


def mine(db: Session, actor: Actor) -> list[BookingRow]:
    me = common.me(db, actor)
    if me is None:
        return []
    return _rows(
        db,
        list(
            db.scalars(
                select(Booking)
                .where(Booking.employee_id == me)
                .order_by(Booking.starts_at.desc())
                .limit(30)
            )
        ),
    )


def waiting(db: Session, actor: Actor) -> list[BookingRow]:
    common.require(db, actor, common.READ)
    return _rows(
        db,
        list(
            db.scalars(
                select(Booking).where(Booking.status == "requested").order_by(Booking.starts_at)
            )
        ),
    )


def upcoming(db: Session, actor: Actor, *, days: int = 14) -> list[BookingRow]:
    common.require(db, actor, common.READ)
    now = clock.now()
    return _rows(
        db,
        list(
            db.scalars(
                select(Booking)
                .where(
                    Booking.status == "approved",
                    Booking.ends_at >= now,
                    Booking.starts_at <= now + timedelta(days=days),
                )
                .order_by(Booking.starts_at)
            )
        ),
    )
