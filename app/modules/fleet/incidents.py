"""Vehicle incidents (ADR 0033). A driver reports one with photos; a Fleet Manager reviews
it, recording the repair cost and any insurance claim, and closes it."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock, numbering
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.identity.schemas import Actor
from app.modules.fleet import common
from app.modules.fleet.models import CLAIM_STATUSES, Incident, IncidentPhoto, Vehicle
from app.modules.hr import service as people

STATUS_LABELS = {"reported": "Reported", "reviewed": "Under review", "closed": "Closed"}
STATUS_TONES = {"reported": "warning", "reviewed": "info", "closed": "neutral"}
CLAIM_LABELS = {
    "none": "Not claimed",
    "filed": "Filed",
    "approved": "Approved",
    "rejected": "Rejected",
    "paid": "Paid",
}


@dataclass(frozen=True, slots=True)
class ReportInput:
    vehicle_id: str
    occurred_at: datetime | None
    place: str
    description: str
    injuries: str
    police_report: str


def report(db: Session, actor: Actor, data: ReportInput) -> uuid.UUID:
    common.require(db, actor, common.USE)
    errors: dict[str, str] = {}
    vehicle = db.get(Vehicle, common.uuid_or_none(data.vehicle_id) or uuid.uuid4())
    if vehicle is None:
        errors["vehicle_id"] = "Choose the vehicle."
    if data.occurred_at is None:
        errors["occurred_at"] = "Enter when it happened."
    elif data.occurred_at > clock.now():
        errors["occurred_at"] = "It can't be in the future."
    if not data.place.strip():
        errors["place"] = "Say where it happened."
    if not data.description.strip():
        errors["description"] = "Say what happened."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert vehicle is not None and data.occurred_at is not None  # noqa: S101
    row = Incident(
        number=numbering.next_number(db, "vehicle_incident"),
        vehicle_id=vehicle.id,
        employee_id=common.me(db, actor),
        occurred_at=data.occurred_at,
        place=data.place.strip()[:200],
        description=data.description.strip(),
        injuries=data.injuries.strip() or None,
        police_report=data.police_report.strip()[:60] or None,
        reported_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    record_by(db, actor, "fleet.incident.report", entity_type="vehicle", entity_id=str(vehicle.id))
    return row.id


def _row(db: Session, incident_id: uuid.UUID, *, lock: bool = False) -> Incident:
    query = select(Incident).where(Incident.id == incident_id)
    row = db.scalars(query.with_for_update() if lock else query).first()
    if row is None:
        raise NotFound()
    return row


def _visible(db: Session, actor: Actor, row: Incident) -> bool:
    return common.can(db, actor, common.READ) or row.reported_by == actor.user_id


def add_photo(
    db: Session,
    actor: Actor,
    incident_id: uuid.UUID,
    *,
    filename: str | None,
    mime: str | None,
    data: bytes,
) -> None:
    row = _row(db, incident_id)
    if not _visible(db, actor, row):
        raise NotFound()
    if row.status == "closed":
        raise Conflict("This incident is closed.")
    file_id = files.upload(
        db,
        actor,
        purpose_key="fleet_photo",
        entity_id=str(row.id),
        filename=filename,
        declared_mime=mime,
        data=data,
    )
    db.add(
        IncidentPhoto(incident_id=row.id, file_id=file_id, name=files.sanitise_filename(filename))
    )
    db.flush()


@dataclass(frozen=True, slots=True)
class ReviewInput:
    repair_cost: str
    claim_number: str
    claim_status: str
    recovered: str
    notes: str


def review(
    db: Session, actor: Actor, incident_id: uuid.UUID, data: ReviewInput, *, close: bool
) -> None:
    common.require(db, actor, common.MANAGE)
    row = _row(db, incident_id, lock=True)
    if row.status == "closed":
        raise Conflict("This incident is closed.")
    errors: dict[str, str] = {}
    cost = common.parse_decimal(data.repair_cost, "repair_cost", errors, label="an amount")
    recovered = common.parse_decimal(data.recovered, "recovered", errors, label="an amount")
    if data.claim_status not in CLAIM_STATUSES:
        errors["claim_status"] = "Choose the claim's status."
    elif data.claim_status != "none" and not data.claim_number.strip():
        errors["claim_number"] = "Enter the insurer's claim number."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row.repair_cost, row.recovered = cost, recovered
    row.claim_number = data.claim_number.strip()[:60] or None
    row.claim_status = data.claim_status
    row.review_notes = data.notes.strip() or None
    row.reviewed_by = actor.user_id
    row.status = "closed" if close else "reviewed"
    if close:
        row.closed_at = clock.now()
    db.flush()
    record_by(
        db,
        actor,
        "fleet.incident.close" if close else "fleet.incident.review",
        entity_type="vehicle",
        entity_id=str(row.vehicle_id),
        changes={"incident": row.number},
    )


@dataclass(frozen=True, slots=True)
class IncidentView:
    incident: Incident
    vehicle: Vehicle
    reporter: str | None
    reviewer: str | None
    photos: list[IncidentPhoto]
    can_review: bool


def get(db: Session, actor: Actor, incident_id: uuid.UUID) -> IncidentView:
    row = _row(db, incident_id)
    if not _visible(db, actor, row):
        raise NotFound()
    vehicle = db.get(Vehicle, row.vehicle_id)
    assert vehicle is not None  # noqa: S101  (foreign key)
    names = common.user_names(db, {row.reported_by, row.reviewed_by})
    return IncidentView(
        row,
        vehicle,
        names.get(row.reported_by) if row.reported_by else None,
        names.get(row.reviewed_by) if row.reviewed_by else None,
        list(db.scalars(select(IncidentPhoto).where(IncidentPhoto.incident_id == row.id))),
        common.can(db, actor, common.MANAGE) and row.status != "closed",
    )


@dataclass(frozen=True, slots=True)
class IncidentRow:
    incident: Incident
    vehicle: str
    driver: str | None


def list_incidents(db: Session, actor: Actor, *, status: str = "") -> list[IncidentRow]:
    query = select(Incident).order_by(Incident.occurred_at.desc()).limit(300)
    if not common.can(db, actor, common.READ):
        query = query.where(Incident.reported_by == actor.user_id)
    if status in STATUS_LABELS:
        query = query.where(Incident.status == status)
    rows = list(db.scalars(query))
    vehicles = {
        v.id: v
        for v in db.scalars(select(Vehicle).where(Vehicle.id.in_({r.vehicle_id for r in rows})))
    }
    names = people.names(db, {r.employee_id for r in rows if r.employee_id})
    return [
        IncidentRow(
            r,
            common.label(vehicles[r.vehicle_id]),
            names.get(r.employee_id) if r.employee_id else None,
        )
        for r in rows
    ]


def ensure_manager(db: Session, actor: Actor) -> None:
    if not common.can(db, actor, common.MANAGE):
        raise PermissionDenied()
