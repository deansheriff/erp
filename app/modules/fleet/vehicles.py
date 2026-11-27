"""Vehicles (ADR 0033): the register, documents, drivers, service plans and the services
done. Plans fall due by distance or time, whichever comes first."""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.identity.schemas import Actor
from app.modules.finance import service as finance
from app.modules.fleet import common
from app.modules.fleet.models import (
    DOCUMENT_KINDS,
    FUELS,
    STATUSES,
    TYPES,
    Driver,
    FuelLog,
    Incident,
    Maintenance,
    ServicePlan,
    Vehicle,
    VehicleDocument,
)
from app.modules.hr import service as people


def _months_later(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            return day.replace(year=year, month=month, day=min(day.day, last))
        except ValueError:
            continue
    return day


# ------------------------------------------------------------------ vehicles


@dataclass(frozen=True, slots=True)
class VehicleInput:
    registration: str
    make: str
    model: str
    year: str
    type: str
    fuel: str
    colour: str
    vin: str
    purchase_date: date | None
    start_odometer: str
    is_pool: bool
    driver_id: str
    notes: str


def save(db: Session, actor: Actor, vehicle_id: uuid.UUID | None, data: VehicleInput) -> uuid.UUID:
    common.require(db, actor, common.MANAGE)
    row = common.vehicle_of(db, vehicle_id, lock=True) if vehicle_id else None
    errors: dict[str, str] = {}
    registration = data.registration.strip().upper()[:20]
    if not registration:
        errors["registration"] = "Enter the registration number."
    elif db.scalar(
        select(Vehicle.id).where(Vehicle.registration == registration, Vehicle.id != vehicle_id)
    ):
        errors["registration"] = "Another vehicle has that registration."
    if not data.make.strip():
        errors["make"] = "Enter the make."
    if not data.model.strip():
        errors["model"] = "Enter the model."
    if data.type not in TYPES:
        errors["type"] = "Choose the type."
    if data.fuel not in FUELS:
        errors["fuel"] = "Choose the fuel."
    year = common.parse_int(data.year, "year", errors, label="a year, like 2021")
    if year is not None and not 1950 <= year <= clock.today().year + 1:
        errors["year"] = "Enter a year, like 2021."
    start = common.parse_int(data.start_odometer, "start_odometer", errors, label="kilometres")
    driver = common.uuid_or_none(data.driver_id)
    if data.driver_id and common.licensed(db, driver, clock.today()) is None:
        errors["driver_id"] = "Choose a driver with a licence in date."
    if errors:
        raise ValidationFailed(field_errors=errors)
    if row is None:
        row = Vehicle(
            code=numbering.next_number(db, "vehicle"),
            registration=registration,
            make=data.make.strip(),
            model=data.model.strip(),
            type=data.type,
            fuel=data.fuel,
        )
        db.add(row)
    row.registration, row.make, row.model = (
        registration,
        data.make.strip()[:60],
        data.model.strip()[:60],
    )
    row.year, row.type, row.fuel = year, data.type, data.fuel
    row.colour, row.vin = data.colour.strip()[:30] or None, data.vin.strip()[:40] or None
    row.purchase_date, row.start_odometer = data.purchase_date, start or 0
    row.is_pool, row.driver_employee_id = data.is_pool, driver
    row.notes = data.notes.strip() or None
    db.flush()
    record_by(db, actor, "fleet.vehicle.save", entity_type="vehicle", entity_id=str(row.id))
    return row.id


def set_status(db: Session, actor: Actor, vehicle_id: uuid.UUID, status: str) -> None:
    common.require(db, actor, common.MANAGE)
    row = common.vehicle_of(db, vehicle_id, lock=True)
    if status not in STATUSES:
        raise ValidationFailed(field_errors={"status": "Choose a status."})
    if row.status == "retired":
        raise Conflict("A retired vehicle stays retired.")
    before, row.status = row.status, status
    db.flush()
    record_by(
        db,
        actor,
        "fleet.vehicle.status",
        entity_type="vehicle",
        entity_id=str(row.id),
        changes={"status": {"from": before, "to": status}},
    )


@dataclass(frozen=True, slots=True)
class DocumentInput:
    kind: str
    number: str
    issuer: str
    issued_on: date | None
    expires_on: date | None


def add_document(
    db: Session,
    actor: Actor,
    vehicle_id: uuid.UUID,
    data: DocumentInput,
    *,
    filename: str | None = None,
    mime: str | None = None,
    content: bytes = b"",
) -> None:
    common.require(db, actor, common.MANAGE)
    row = common.vehicle_of(db, vehicle_id)
    errors: dict[str, str] = {}
    if data.kind not in DOCUMENT_KINDS:
        errors["kind"] = "Choose the document."
    if data.expires_on is None:
        errors["expires_on"] = "Enter when it expires."
    elif data.issued_on and data.expires_on < data.issued_on:
        errors["expires_on"] = "It can't expire before it was issued."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert data.expires_on is not None  # noqa: S101
    file_id = None
    if content:
        file_id = files.upload(
            db,
            actor,
            purpose_key="fleet_document",
            entity_id=str(row.id),
            filename=filename,
            declared_mime=mime,
            data=content,
        )
    db.add(
        VehicleDocument(
            vehicle_id=row.id,
            kind=data.kind,
            number=data.number.strip()[:60] or None,
            issuer=data.issuer.strip()[:120] or None,
            issued_on=data.issued_on,
            expires_on=data.expires_on,
            file_id=file_id,
            created_by=actor.user_id,
        )
    )
    db.flush()
    record_by(
        db,
        actor,
        "fleet.document.add",
        entity_type="vehicle",
        entity_id=str(row.id),
        changes={"kind": data.kind, "expires": data.expires_on.isoformat()},
    )


# ------------------------------------------------------------------ drivers


@dataclass(frozen=True, slots=True)
class DriverInput:
    employee_id: str
    licence_number: str
    licence_class: str
    licence_expiry: date | None
    active: bool


def save_driver(db: Session, actor: Actor, data: DriverInput) -> uuid.UUID:
    common.require(db, actor, common.MANAGE)
    errors: dict[str, str] = {}
    employee = common.uuid_or_none(data.employee_id)
    if employee not in set(people.current_ids(db)):
        errors["employee_id"] = "Choose someone on the staff list."
    if not data.licence_number.strip():
        errors["licence_number"] = "Enter the licence number."
    if data.licence_expiry is None:
        errors["licence_expiry"] = "Enter when the licence expires."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert employee is not None and data.licence_expiry is not None  # noqa: S101
    row = db.scalars(select(Driver).where(Driver.employee_id == employee)).first()
    if row is None:
        row = Driver(
            employee_id=employee,
            licence_number=data.licence_number.strip(),
            licence_expiry=data.licence_expiry,
        )
        db.add(row)
    row.licence_number = data.licence_number.strip()[:40]
    row.licence_class = data.licence_class.strip()[:10] or None
    row.licence_expiry, row.is_active = data.licence_expiry, data.active
    db.flush()
    record_by(db, actor, "fleet.driver.save", entity_type="employee", entity_id=str(employee))
    return row.id


@dataclass(frozen=True, slots=True)
class DriverRow:
    driver: Driver
    name: str
    vehicles: list[str]
    state: str  # valid, soon, expired


def driver_rows(db: Session, actor: Actor) -> list[DriverRow]:
    common.require(db, actor, common.READ)
    drivers = list(db.scalars(select(Driver)))
    names = people.names(db, {d.employee_id for d in drivers})
    assigned: dict[uuid.UUID, list[str]] = {}
    for v in db.scalars(select(Vehicle).where(Vehicle.driver_employee_id.is_not(None))):
        assigned.setdefault(v.driver_employee_id, []).append(v.registration)  # type: ignore[arg-type]
    today, soon = clock.today(), common.settings(db).first_alert_days
    return sorted(
        (
            DriverRow(
                d,
                names.get(d.employee_id, ""),
                assigned.get(d.employee_id, []),
                "expired"
                if d.licence_expiry < today
                else ("soon" if (d.licence_expiry - today).days <= soon else "valid"),
            )
            for d in drivers
        ),
        key=lambda r: r.name,
    )


def driver_choices(db: Session) -> list[tuple[uuid.UUID, str]]:
    """People with an active driver record and a licence in date."""
    today = clock.today()
    drivers = [
        d for d in db.scalars(select(Driver).where(Driver.is_active)) if d.licence_expiry >= today
    ]
    names = people.names(db, {d.employee_id for d in drivers})
    return sorted(
        ((d.employee_id, names.get(d.employee_id, "")) for d in drivers), key=lambda p: p[1]
    )


def staff_choices(db: Session) -> list[tuple[uuid.UUID, str]]:
    """Everyone still employed, for adding a driver record."""
    return [(e, name) for e, name, _ in people.assignable_people(db)]


# ------------------------------------------------------------------ service plans and services


@dataclass(frozen=True, slots=True)
class PlanState:
    plan: ServicePlan
    due_km: int | None
    due_on: date | None
    km_left: int | None
    days_left: int | None

    @property
    def overdue(self) -> bool:
        return (self.km_left is not None and self.km_left <= 0) or (
            self.days_left is not None and self.days_left <= 0
        )


def plan_state(db: Session, vehicle: Vehicle, plan: ServicePlan, reading: int) -> PlanState:
    base_km = plan.last_done_km if plan.last_done_km is not None else vehicle.start_odometer
    base_day = plan.last_done_on or vehicle.purchase_date or clock.local_date(vehicle.created_at)
    due_km = base_km + plan.every_km if plan.every_km else None
    due_on = _months_later(base_day, plan.every_months) if plan.every_months else None
    return PlanState(
        plan,
        due_km,
        due_on,
        due_km - reading if due_km is not None else None,
        (due_on - clock.today()).days if due_on else None,
    )


def save_plan(
    db: Session,
    actor: Actor,
    vehicle_id: uuid.UUID,
    *,
    name: str,
    every_km: str,
    every_months: str,
) -> None:
    common.require(db, actor, common.MANAGE)
    row = common.vehicle_of(db, vehicle_id)
    errors: dict[str, str] = {}
    km = common.parse_int(every_km, "every_km", errors, label="kilometres, like 5000")
    months = common.parse_int(every_months, "every_months", errors, label="months, like 3")
    if not name.strip():
        errors["plan_name"] = "Name the service, like Oil change."
    if not errors and not km and not months:
        errors["every_km"] = "Give a distance, a number of months, or both."
    if errors:
        raise ValidationFailed(field_errors=errors)
    db.add(
        ServicePlan(
            vehicle_id=row.id,
            name=name.strip()[:80],
            every_km=km or None,
            every_months=months or None,
        )
    )
    db.flush()


@dataclass(frozen=True, slots=True)
class ServiceInput:
    plan_id: str
    day: date | None
    odometer: str
    description: str
    vendor: str
    cost: str
    reference: str


def record_service(db: Session, actor: Actor, vehicle_id: uuid.UUID, data: ServiceInput) -> None:
    """A scheduled service (against a plan, which it resets) or an ad-hoc one."""
    common.require(db, actor, common.MANAGE)
    row = common.vehicle_of(db, vehicle_id, lock=True)
    errors: dict[str, str] = {}
    plan = db.get(ServicePlan, common.uuid_or_none(data.plan_id) or uuid.uuid4())
    if data.plan_id and (plan is None or plan.vehicle_id != row.id):
        errors["plan_id"] = "Choose one of this vehicle's plans."
    if data.day is None:
        errors["service_day"] = "Enter the date."
    elif data.day > clock.today():
        errors["service_day"] = "The date can't be in the future."
    reading = common.parse_int(data.odometer, "service_odometer", errors, label="kilometres")
    cost = common.parse_decimal(data.cost, "service_cost", errors, label="an amount")
    description = data.description.strip() or (plan.name if plan else "")
    if not description:
        errors["service_description"] = "Say what was done."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert data.day is not None  # noqa: S101
    db.add(
        Maintenance(
            vehicle_id=row.id,
            plan_id=plan.id if plan else None,
            day=data.day,
            odometer=reading,
            description=description[:300],
            vendor=data.vendor.strip()[:120] or None,
            cost=cost,
            reference=data.reference.strip()[:40] or None,
            created_by=actor.user_id,
        )
    )
    if plan is not None:
        plan.last_done_on, plan.last_done_km = data.day, reading
        plan.alerted_for = None
    db.flush()
    record_by(db, actor, "fleet.service.record", entity_type="vehicle", entity_id=str(row.id))


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class VehicleRow:
    vehicle: Vehicle
    driver: str | None
    odometer: int
    documents: str  # valid, soon, expired, missing
    document_note: str | None
    next_service: PlanState | None


def _document_state(db: Session, vehicle_id: uuid.UUID) -> tuple[str, str | None]:
    found = common.current_documents(db, vehicle_id)
    if not found:
        return "missing", "No documents yet"
    today, soon = clock.today(), common.settings(db).first_alert_days
    worst: tuple[str, str | None] = ("valid", None)
    for kind, doc in found.items():
        name = common.DOCUMENT_LABELS[kind]
        if doc.expires_on < today:
            return "expired", f"{name} expired {doc.expires_on:%d %b %Y}"
        if (doc.expires_on - today).days <= soon:
            worst = ("soon", f"{name} expires {doc.expires_on:%d %b %Y}")
    return worst


def list_vehicles(
    db: Session, actor: Actor, *, status: str = "", pool: bool = False
) -> list[VehicleRow]:
    common.require(db, actor, common.READ)
    query = select(Vehicle).order_by(Vehicle.code)
    if status in STATUSES:
        query = query.where(Vehicle.status == status)
    elif not status:
        query = query.where(Vehicle.status != "retired")
    if pool:
        query = query.where(Vehicle.is_pool)
    rows = list(db.scalars(query))
    names = people.names(db, {r.driver_employee_id for r in rows if r.driver_employee_id})
    out = []
    for r in rows:
        reading = common.odometer(db, r)
        plans = [
            plan_state(db, r, p, reading)
            for p in db.scalars(
                select(ServicePlan).where(ServicePlan.vehicle_id == r.id, ServicePlan.is_active)
            )
        ]
        nearest = min(plans, key=_urgency, default=None)
        state, note = _document_state(db, r.id)
        out.append(
            VehicleRow(
                r,
                names.get(r.driver_employee_id) if r.driver_employee_id else None,
                reading,
                state,
                note,
                nearest,
            )
        )
    return out


def _urgency(state: PlanState) -> float:
    """Lower is sooner: kilometres left, or days left as about 100 km a day."""
    options = []
    if state.km_left is not None:
        options.append(float(state.km_left))
    if state.days_left is not None:
        options.append(float(state.days_left) * 100)
    return min(options) if options else float("inf")


@dataclass(frozen=True, slots=True)
class VehicleView:
    vehicle: Vehicle
    driver: str | None
    odometer: int
    documents: list[VehicleDocument]
    plans: list[PlanState]
    services: list[Maintenance]
    fuel: list[FuelLog]
    incidents: list[Incident]
    costs: list[finance.VehicleCost] = field(default_factory=list)
    average_km_per_litre: Decimal | None = None
    can_manage: bool = False
    can_costs: bool = False


def get(db: Session, actor: Actor, vehicle_id: uuid.UUID) -> VehicleView:
    common.require(db, actor, common.READ)
    row = common.vehicle_of(db, vehicle_id)
    reading = common.odometer(db, row)
    fuel = list(
        db.scalars(
            select(FuelLog)
            .where(FuelLog.vehicle_id == row.id)
            .order_by(FuelLog.day.desc(), FuelLog.odometer.desc())
            .limit(50)
        )
    )
    rates = [f.km_per_litre for f in fuel if f.km_per_litre]
    average = (sum(rates, Decimal(0)) / len(rates)).quantize(Decimal("0.1")) if rates else None
    costs_ok = common.can(db, actor, common.COSTS)
    return VehicleView(
        row,
        people.names(db, {row.driver_employee_id}).get(row.driver_employee_id)
        if row.driver_employee_id
        else None,
        reading,
        list(
            db.scalars(
                select(VehicleDocument)
                .where(VehicleDocument.vehicle_id == row.id)
                .order_by(VehicleDocument.expires_on.desc())
            )
        ),
        [
            plan_state(db, row, p, reading)
            for p in db.scalars(
                select(ServicePlan)
                .where(ServicePlan.vehicle_id == row.id)
                .order_by(ServicePlan.name)
            )
        ],
        list(
            db.scalars(
                select(Maintenance)
                .where(Maintenance.vehicle_id == row.id)
                .order_by(Maintenance.day.desc())
                .limit(50)
            )
        ),
        fuel,
        list(
            db.scalars(
                select(Incident)
                .where(Incident.vehicle_id == row.id)
                .order_by(Incident.occurred_at.desc())
            )
        ),
        finance.vehicle_costs(db, {row.id}, start=date(clock.today().year, 1, 1))
        if costs_ok
        else [],
        average,
        common.can(db, actor, common.MANAGE),
        costs_ok,
    )


def choices(db: Session, *, pool_only: bool = False) -> list[tuple[uuid.UUID, str]]:
    query = select(Vehicle).where(Vehicle.status == "active").order_by(Vehicle.registration)
    if pool_only:
        query = query.where(Vehicle.is_pool)
    return [(v.id, f"{v.code} · {common.label(v)}") for v in db.scalars(query)]


@dataclass(frozen=True, slots=True)
class Link:
    """Finance's view of vehicles (finance.vehicle_link.VehicleLink)."""

    def choices(self, db: Session) -> list[tuple[uuid.UUID, str]]:
        return [
            (v.id, f"{v.code} · {common.label(v)}")
            for v in db.scalars(
                select(Vehicle).where(Vehicle.status != "retired").order_by(Vehicle.code)
            )
        ]

    def is_open(self, db: Session, vehicle_id: uuid.UUID) -> bool:
        row = db.get(Vehicle, vehicle_id)
        return row is not None and row.status != "retired"

    def labels(self, db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        return {
            v.id: f"{v.code} · {common.label(v)}"
            for v in db.scalars(select(Vehicle).where(Vehicle.id.in_(ids)))
        }


def check_manager(db: Session, actor: Actor) -> None:
    if not common.can(db, actor, common.MANAGE):
        raise PermissionDenied()


def found(db: Session, vehicle_id: uuid.UUID) -> Vehicle:
    row = db.get(Vehicle, vehicle_id)
    if row is None:
        raise NotFound()
    return row
