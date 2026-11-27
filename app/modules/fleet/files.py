"""File purposes for fleet (ADR 0033): vehicle document scans (on the vehicle), fuel
receipts (on the fuel log) and incident photos (on the incident). A receipt or photo is
owned by the employee who logged or reported it, so they can attach and see their own;
Fleet Managers hold `fleet.vehicle.use` organisation-wide and see them all."""

import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.fleet.models import FuelLog, Incident, Vehicle

IMAGES_AND_PDF = frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"})
IMAGES = frozenset({"image/png", "image/jpeg", "image/webp"})


def _id(entity_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(entity_id)
    except ValueError:
        return None


def _vehicle(db: Session, entity_id: str) -> Resource | None:
    key = _id(entity_id)
    return Resource() if key and db.get(Vehicle, key) else None


def _fuel_log(db: Session, entity_id: str) -> Resource | None:
    key = _id(entity_id)
    row = db.get(FuelLog, key) if key else None
    return Resource(owner_employee_id=row.employee_id) if row else None


def _incident(db: Session, entity_id: str) -> Resource | None:
    key = _id(entity_id)
    row = db.get(Incident, key) if key else None
    return Resource(owner_employee_id=row.employee_id) if row else None


def _purpose(
    key: str,
    label: str,
    entity_type: str,
    resolve: Callable[[Session, str], Resource | None],
    *,
    attach: str,
    view: str,
    types: frozenset[str] = IMAGES_AND_PDF,
) -> FilePurpose:
    return FilePurpose(
        key=key,
        label=label,
        entity_type=entity_type,
        allowed_types=types,
        max_bytes=10 * MB,
        attach_permission=attach,
        view_permission=view,
        # Kept six years, like other records of spending (confirm).
        retention=timedelta(days=6 * 365),
        inline=True,
        classification="directory",
        resolve=resolve,
    )


FLEET_DOCUMENT = _purpose(
    "fleet_document",
    "Vehicle document",
    "vehicle",
    _vehicle,
    attach="fleet.vehicle.manage",
    view="fleet.vehicle.read",
)
FLEET_RECEIPT = _purpose(
    "fleet_receipt",
    "Fuel receipt",
    "fleet_fuel_log",
    _fuel_log,
    attach="fleet.vehicle.use",
    view="fleet.vehicle.use",
)
FLEET_PHOTO = _purpose(
    "fleet_photo",
    "Incident photo",
    "fleet_incident",
    _incident,
    attach="fleet.vehicle.use",
    view="fleet.vehicle.use",
    types=IMAGES,
)

PURPOSES = (FLEET_DOCUMENT, FLEET_RECEIPT, FLEET_PHOTO)
