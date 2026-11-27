"""Fleet web routes (ADR 0033): my fleet (fuel, bookings, incident reports), the vehicle
register and a vehicle's page, booking approvals, incidents, drivers, cost per vehicle
and settings. Routes translate forms and render; the services do the work."""

import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, screen
from app.core import clock
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.fleet import alerts, bookings, common, costs, fuel, incidents, vehicles
from app.modules.fleet.bookings import BookingInput
from app.modules.fleet.files import FLEET_DOCUMENT, FLEET_PHOTO, FLEET_RECEIPT
from app.modules.fleet.fuel import FuelInput
from app.modules.fleet.incidents import ReportInput, ReviewInput
from app.modules.fleet.models import CLAIM_STATUSES, DOCUMENT_KINDS, FUELS, STATUSES, TYPES
from app.modules.fleet.vehicles import DocumentInput, DriverInput, ServiceInput, VehicleInput
from app.web.access import require_actor
from app.web.forms import all_fields, parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
Fields = Annotated[dict[str, str], Depends(all_fields)]
ReceiptUpload = Annotated[ReceivedUpload, Depends(upload_body(FLEET_RECEIPT.max_bytes))]
DocumentUpload = Annotated[ReceivedUpload, Depends(upload_body(FLEET_DOCUMENT.max_bytes))]
PhotoUpload = Annotated[ReceivedUpload, Depends(upload_body(FLEET_PHOTO.max_bytes))]
DomainProblem = ValidationFailed | Conflict | PermissionDenied
NOTICES = {
    "fuel": "Fuel logged.",
    "flagged": "Fuel logged. This fill used more fuel than usual, so it's been flagged.",
    "requested": "Booking requested. A Fleet Manager will confirm it.",
    "cancelled": "Booking cancelled.",
    "approved": "Booking approved. The requester sees it on My fleet.",
    "rejected": "Booking rejected. The requester sees the reason on My fleet.",
    "reported": "Incident reported. A Fleet Manager will review it.",
    "photo": "Photo added. It's checked for viruses before anyone can open it.",
    "reviewed": "Review saved.",
    "closed": "Incident closed.",
    "saved": "Saved.",
    "document": "Document added.",
    "plan": "Service plan saved.",
    "service": "Service recorded. The plan's next due point has moved on.",
    "status": "Status changed.",
    "driver": "Driver saved.",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str, *crumbs: str) -> Any:
    return build_shell(
        db,
        actor,
        active_module="fleet",
        active_section=section,
        crumbs=["Fleet", section, *crumbs],
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _moment(value: str | None) -> datetime | None:
    """An `<input type="datetime-local">` value in the organisation's timezone."""
    try:
        moment = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if moment is None:
        return None
    return moment.replace(tzinfo=ZoneInfo(get_settings().timezone))


def _upload_problem(upload: ReceivedUpload, key: str) -> None:
    if upload.errors:
        raise ValidationFailed(field_errors={key: next(iter(upload.errors.values()))})


def _labels() -> dict[str, Any]:
    return {
        "type_labels": common.TYPE_LABELS,
        "fuel_labels": common.FUEL_LABELS,
        "status_labels": common.STATUS_LABELS,
        "status_tones": common.STATUS_TONES,
        "document_labels": common.DOCUMENT_LABELS,
        "booking_labels": bookings.STATUS_LABELS,
        "booking_tones": bookings.STATUS_TONES,
        "incident_labels": incidents.STATUS_LABELS,
        "incident_tones": incidents.STATUS_TONES,
        "claim_labels": incidents.CLAIM_LABELS,
    }


# ------------------------------------------------------------------ my fleet


def _my_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    mine = fuel.my_vehicles(db, actor)
    return {
        "shell": _shell(db, actor, "My fleet"),
        "mine": [(v, common.odometer(db, v)) for v in mine],
        "logs": fuel.recent(db, actor, limit=10),
        "bookings": bookings.mine(db, actor),
        "pool": vehicles.choices(db, pool_only=True),
        "is_manager": common.can(db, actor, common.MANAGE),
        "today": clock.today(),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "My fleet",
        **_labels(),
        **extra,
    }


@router.get("/fleet", response_class=HTMLResponse)
@requires("fleet.vehicle.use")
@screen("fleet.my")
def my_fleet(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _my_context(db, actor, notice=NOTICES.get(notice))
    return _render(request, "fleet/my.html", context)


@router.post("/fleet/fuel", response_class=HTMLResponse)
@requires("fleet.vehicle.use")
@screen("fleet.my")
def log_fuel(request: Request, db: Db, actor: CurrentActor, upload: ReceiptUpload) -> Response:
    f = upload.fields
    data = FuelInput(
        f.get("vehicle_id", ""),
        parse_day(f.get("fuel_day")),
        f.get("litres", ""),
        f.get("fuel_amount", ""),
        f.get("fuel_odometer", ""),
        f.get("station", ""),
        f.get("fuel_reference", ""),
    )
    try:
        _upload_problem(upload, "receipt")
        with db.begin_nested():
            log_id = fuel.log(
                db,
                actor,
                data,
                filename=upload.filename,
                mime=upload.content_type,
                receipt=upload.data,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _my_context(db, actor, values=f, errors=_errors(exc), form="fuel")
        return _render(request, "fleet/my.html", context, 422)
    if upload.data:
        files.request_scan()
    flagged = any(r.log.id == log_id and r.log.flagged for r in fuel.recent(db, actor, limit=5))
    return _see_other(f"/fleet?notice={'flagged' if flagged else 'fuel'}")


@router.post("/fleet/bookings/new", response_class=HTMLResponse)
@requires("fleet.vehicle.use")
@screen("fleet.my")
def request_booking(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    data = BookingInput(
        f.get("booking_vehicle_id", ""),
        _moment(f.get("starts_at")),
        _moment(f.get("ends_at")),
        f.get("purpose", ""),
        f.get("destination", ""),
    )
    try:
        with db.begin_nested():
            bookings.request(db, actor, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = _errors(exc)
        if "vehicle_id" in errors:
            errors["booking_vehicle_id"] = errors.pop("vehicle_id")
        context = _my_context(db, actor, values=f, errors=errors, form="booking")
        return _render(request, "fleet/my.html", context, 422)
    return _see_other("/fleet?notice=requested")


@router.post("/fleet/bookings/{booking_id:uuid}/cancel", response_class=HTMLResponse)
@requires("fleet.vehicle.use")
@screen("fleet.my")
def cancel_booking(
    request: Request, db: Db, actor: CurrentActor, booking_id: uuid.UUID, f: Fields
) -> Response:
    back = "/fleet/bookings" if f.get("back") == "bookings" else "/fleet"
    try:
        with db.begin_nested():
            bookings.cancel(db, actor, booking_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _my_context(db, actor, errors=_errors(exc))
        return _render(request, "fleet/my.html", context, 422)
    return _see_other(f"{back}?notice=cancelled")


@router.post("/fleet/incidents/new", response_class=HTMLResponse)
@requires("fleet.vehicle.use")
@screen("fleet.my")
def report_incident(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    data = ReportInput(
        f.get("incident_vehicle_id", ""),
        _moment(f.get("occurred_at")),
        f.get("place", ""),
        f.get("description", ""),
        f.get("injuries", ""),
        f.get("police_report", ""),
    )
    try:
        with db.begin_nested():
            incident = incidents.report(db, actor, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = _errors(exc)
        if "vehicle_id" in errors:
            errors["incident_vehicle_id"] = errors.pop("vehicle_id")
        context = _my_context(db, actor, values=f, errors=errors, form="incident")
        return _render(request, "fleet/my.html", context, 422)
    return _see_other(f"/fleet/incidents/{incident}?notice=reported")


# ------------------------------------------------------------------ vehicles


@router.get("/fleet/vehicles", response_class=HTMLResponse)
@requires("fleet.vehicle.read")
@screen("fleet.vehicles")
def vehicle_list(
    request: Request, db: Db, actor: CurrentActor, status: str = "", pool: str = ""
) -> HTMLResponse:
    rows = vehicles.list_vehicles(db, actor, status=status, pool=bool(pool))
    context = {
        "shell": _shell(db, actor, "Vehicles"),
        "rows": rows,
        "status": status,
        "pool": bool(pool),
        "statuses": STATUSES,
        "due": alerts.documents_due(db, clock.today(), common.settings(db).first_alert_days),
        "today": clock.today(),
        "can_manage": common.can(db, actor, common.MANAGE),
        "page_title": "Vehicles",
        **_labels(),
    }
    return _render(request, "fleet/vehicles.html", context)


def _form_context(
    db: Session, actor: Actor, vehicle_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    vehicles.check_manager(db, actor)
    row = vehicles.found(db, vehicle_id) if vehicle_id else None
    values: dict[str, Any] = {"type": "car", "fuel": "petrol", "start_odometer": "0"}
    if row is not None:
        values = {
            "registration": row.registration,
            "make": row.make,
            "model": row.model,
            "year": str(row.year or ""),
            "type": row.type,
            "fuel": row.fuel,
            "colour": row.colour or "",
            "vin": row.vin or "",
            "purchase_date": row.purchase_date.isoformat() if row.purchase_date else "",
            "start_odometer": str(row.start_odometer),
            "is_pool": "1" if row.is_pool else "",
            "driver_id": str(row.driver_employee_id or ""),
            "notes": row.notes or "",
        }
    return {
        "shell": _shell(db, actor, "Vehicles", row.code if row else "New vehicle"),
        "vehicle": row,
        "types": TYPES,
        "fuels": FUELS,
        "drivers": vehicles.driver_choices(db),
        "values": values,
        "errors": {},
        "page_title": f"Edit {row.registration}" if row else "Add a vehicle",
        **_labels(),
        **extra,
    }


@router.get("/fleet/vehicles/new", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.vehicle_form")
def new_vehicle(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "fleet/vehicle_form.html", _form_context(db, actor, None))


@router.get("/fleet/vehicles/{vehicle_id:uuid}/edit", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.vehicle_form")
def edit_vehicle(
    request: Request, db: Db, actor: CurrentActor, vehicle_id: uuid.UUID
) -> HTMLResponse:
    return _render(request, "fleet/vehicle_form.html", _form_context(db, actor, vehicle_id))


def _save_vehicle(
    request: Request, db: Session, actor: Actor, vehicle_id: uuid.UUID | None, f: dict[str, str]
) -> Response:
    data = VehicleInput(
        f.get("registration", ""),
        f.get("make", ""),
        f.get("model", ""),
        f.get("year", ""),
        f.get("type", ""),
        f.get("fuel", ""),
        f.get("colour", ""),
        f.get("vin", ""),
        parse_day(f.get("purchase_date")),
        f.get("start_odometer", ""),
        bool(f.get("is_pool")),
        f.get("driver_id", ""),
        f.get("notes", ""),
    )
    try:
        with db.begin_nested():
            saved = vehicles.save(db, actor, vehicle_id, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _form_context(db, actor, vehicle_id, values=f, errors=_errors(exc))
        return _render(request, "fleet/vehicle_form.html", context, 422)
    return _see_other(f"/fleet/vehicles/{saved}?notice=saved")


@router.post("/fleet/vehicles/new", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.vehicle_form")
def create_vehicle(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    return _save_vehicle(request, db, actor, None, f)


@router.post("/fleet/vehicles/{vehicle_id:uuid}/edit", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.vehicle_form")
def update_vehicle(
    request: Request, db: Db, actor: CurrentActor, vehicle_id: uuid.UUID, f: Fields
) -> Response:
    return _save_vehicle(request, db, actor, vehicle_id, f)


def _vehicle_context(
    db: Session, actor: Actor, vehicle_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    view = vehicles.get(db, actor, vehicle_id)
    return {
        "shell": _shell(db, actor, "Vehicles", view.vehicle.code),
        "view": view,
        "v": view.vehicle,
        "today": clock.today(),
        "soon": common.settings(db).first_alert_days,
        "ytd": sum((c.net for c in view.costs), common.ZERO),
        "kinds": DOCUMENT_KINDS,
        "statuses": STATUSES,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"{view.vehicle.registration} {view.vehicle.make} {view.vehicle.model}",
        **_labels(),
        **extra,
    }


@router.get("/fleet/vehicles/{vehicle_id:uuid}", response_class=HTMLResponse)
@requires("fleet.vehicle.read")
@screen("fleet.vehicle")
def vehicle_page(
    request: Request, db: Db, actor: CurrentActor, vehicle_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _vehicle_context(db, actor, vehicle_id, notice=NOTICES.get(notice))
    return _render(request, "fleet/vehicle.html", context)


@router.post("/fleet/vehicles/{vehicle_id:uuid}/documents", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.vehicle")
def add_document(
    request: Request, db: Db, actor: CurrentActor, vehicle_id: uuid.UUID, upload: DocumentUpload
) -> Response:
    f = upload.fields
    data = DocumentInput(
        f.get("kind", ""),
        f.get("number", ""),
        f.get("issuer", ""),
        parse_day(f.get("issued_on")),
        parse_day(f.get("expires_on")),
    )
    try:
        _upload_problem(upload, "file")
        with db.begin_nested():
            vehicles.add_document(
                db,
                actor,
                vehicle_id,
                data,
                filename=upload.filename,
                mime=upload.content_type,
                content=upload.data,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _vehicle_context(
            db, actor, vehicle_id, values=f, errors=_errors(exc), form="document"
        )
        return _render(request, "fleet/vehicle.html", context, 422)
    if upload.data:
        files.request_scan()
    return _see_other(f"/fleet/vehicles/{vehicle_id}?notice=document")


def _vehicle_action(
    db: Session, actor: Actor, vehicle_id: uuid.UUID, action: str, f: dict[str, str]
) -> str:
    """One action on a vehicle's page; returns the notice key."""
    runs: dict[str, tuple[Callable[[], object], str]] = {
        "status": (
            lambda: vehicles.set_status(db, actor, vehicle_id, f.get("status", "")),
            "status",
        ),
        "plans": (
            lambda: vehicles.save_plan(
                db,
                actor,
                vehicle_id,
                name=f.get("plan_name", ""),
                every_km=f.get("every_km", ""),
                every_months=f.get("every_months", ""),
            ),
            "plan",
        ),
        "services": (
            lambda: vehicles.record_service(
                db,
                actor,
                vehicle_id,
                ServiceInput(
                    f.get("plan_id", ""),
                    parse_day(f.get("service_day")),
                    f.get("service_odometer", ""),
                    f.get("service_description", ""),
                    f.get("vendor", ""),
                    f.get("service_cost", ""),
                    f.get("service_reference", ""),
                ),
            ),
            "service",
        ),
    }
    if action not in runs:
        raise NotFound()
    run, notice = runs[action]
    run()
    return notice


@router.post("/fleet/vehicles/{vehicle_id:uuid}/{action}", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.vehicle")
def vehicle_action(
    request: Request, db: Db, actor: CurrentActor, vehicle_id: uuid.UUID, action: str, f: Fields
) -> Response:
    try:
        with db.begin_nested():
            notice = _vehicle_action(db, actor, vehicle_id, action, f)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _vehicle_context(
            db, actor, vehicle_id, values=f, errors=_errors(exc), form=action
        )
        return _render(request, "fleet/vehicle.html", context, 422)
    return _see_other(f"/fleet/vehicles/{vehicle_id}?notice={notice}")


# ------------------------------------------------------------------ bookings


def _bookings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    vehicles.check_manager(db, actor)
    return {
        "shell": _shell(db, actor, "Bookings"),
        "waiting": bookings.waiting(db, actor),
        "upcoming": bookings.upcoming(db, actor),
        "drivers": vehicles.driver_choices(db),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Vehicle bookings",
        **_labels(),
        **extra,
    }


@router.get("/fleet/bookings", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.bookings")
def booking_list(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _bookings_context(db, actor, notice=NOTICES.get(notice))
    return _render(request, "fleet/bookings.html", context)


@router.post("/fleet/bookings/{booking_id:uuid}/decide", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.bookings")
def decide_booking(
    request: Request, db: Db, actor: CurrentActor, booking_id: uuid.UUID, f: Fields
) -> Response:
    approve = f.get("decision") == "approve"
    try:
        with db.begin_nested():
            bookings.decide(
                db,
                actor,
                booking_id,
                approve=approve,
                driver_id=f.get(f"driver_{booking_id}", ""),
                reason=f.get(f"reason_{booking_id}", ""),
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = _errors(exc)
        if "form" in errors:
            errors[f"row_{booking_id}"] = errors.pop("form")
        context = _bookings_context(db, actor, values=f, errors=errors)
        return _render(request, "fleet/bookings.html", context, 422)
    return _see_other(f"/fleet/bookings?notice={'approved' if approve else 'rejected'}")


# ------------------------------------------------------------------ incidents


@router.get("/fleet/incidents", response_class=HTMLResponse)
@requires("fleet.vehicle.use")
@screen("fleet.incidents")
def incident_list(request: Request, db: Db, actor: CurrentActor, status: str = "") -> HTMLResponse:
    context = {
        "shell": _shell(db, actor, "Incidents"),
        "rows": incidents.list_incidents(db, actor, status=status),
        "status": status,
        "is_manager": common.can(db, actor, common.READ),
        "page_title": "Vehicle incidents",
        **_labels(),
    }
    return _render(request, "fleet/incidents.html", context)


def _incident_context(
    db: Session, actor: Actor, incident_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    view = incidents.get(db, actor, incident_id)
    return {
        "shell": _shell(db, actor, "Incidents", view.incident.number),
        "view": view,
        "i": view.incident,
        "claims": CLAIM_STATUSES,
        "label": common.label(view.vehicle),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": f"Incident {view.incident.number}",
        **_labels(),
        **extra,
    }


@router.get("/fleet/incidents/{incident_id:uuid}", response_class=HTMLResponse)
@requires("fleet.vehicle.use")
@screen("fleet.incident")
def incident_page(
    request: Request, db: Db, actor: CurrentActor, incident_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    context = _incident_context(db, actor, incident_id, notice=NOTICES.get(notice))
    return _render(request, "fleet/incident.html", context)


@router.post("/fleet/incidents/{incident_id:uuid}/photos", response_class=HTMLResponse)
@requires("fleet.vehicle.use")
@screen("fleet.incident")
def add_photo(
    request: Request, db: Db, actor: CurrentActor, incident_id: uuid.UUID, upload: PhotoUpload
) -> Response:
    try:
        _upload_problem(upload, "photo")
        if not upload.data:
            raise ValidationFailed(field_errors={"photo": "Choose a photo."})
        with db.begin_nested():
            incidents.add_photo(
                db,
                actor,
                incident_id,
                filename=upload.filename,
                mime=upload.content_type,
                data=upload.data,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _incident_context(db, actor, incident_id, errors=_errors(exc))
        return _render(request, "fleet/incident.html", context, 422)
    files.request_scan()
    return _see_other(f"/fleet/incidents/{incident_id}?notice=photo")


@router.post("/fleet/incidents/{incident_id:uuid}/review", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.incident")
def review_incident(
    request: Request, db: Db, actor: CurrentActor, incident_id: uuid.UUID, f: Fields
) -> Response:
    close = f.get("then") == "close"
    data = ReviewInput(
        f.get("repair_cost", ""),
        f.get("claim_number", ""),
        f.get("claim_status", ""),
        f.get("recovered", ""),
        f.get("review_notes", ""),
    )
    try:
        with db.begin_nested():
            incidents.review(db, actor, incident_id, data, close=close)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _incident_context(db, actor, incident_id, values=f, errors=_errors(exc))
        return _render(request, "fleet/incident.html", context, 422)
    return _see_other(f"/fleet/incidents/{incident_id}?notice={'closed' if close else 'reviewed'}")


# ------------------------------------------------------------------ drivers, costs, settings


def _drivers_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor, "Drivers"),
        "rows": vehicles.driver_rows(db, actor),
        "staff": vehicles.staff_choices(db),
        "can_manage": common.can(db, actor, common.MANAGE),
        "editing": extra.pop("editing", ""),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Drivers",
        **extra,
    }


@router.get("/fleet/drivers", response_class=HTMLResponse)
@requires("fleet.vehicle.read")
@screen("fleet.drivers")
def driver_list(
    request: Request, db: Db, actor: CurrentActor, edit: str = "", notice: str = ""
) -> HTMLResponse:
    context = _drivers_context(db, actor, editing=edit, notice=NOTICES.get(notice))
    return _render(request, "fleet/drivers.html", context)


@router.post("/fleet/drivers", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.drivers")
def save_driver(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    data = DriverInput(
        f.get("employee_id", ""),
        f.get("licence_number", ""),
        f.get("licence_class", ""),
        parse_day(f.get("licence_expiry")),
        bool(f.get("active")),
    )
    try:
        with db.begin_nested():
            vehicles.save_driver(db, actor, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _drivers_context(
            db, actor, values=f, errors=_errors(exc), editing=f.get("employee_id", "")
        )
        return _render(request, "fleet/drivers.html", context, 422)
    return _see_other("/fleet/drivers?notice=driver")


def _period(start: str, end: str) -> tuple[date, date]:
    today = clock.today()
    first = parse_day(start) or date(today.year, 1, 1)
    last = parse_day(end) or today
    return (first, last) if first <= last else (last, first)


@router.get("/fleet/costs", response_class=HTMLResponse)
@requires("fleet.cost.read")
@screen("fleet.costs")
def cost_report(
    request: Request, db: Db, actor: CurrentActor, start: str = "", end: str = ""
) -> HTMLResponse:
    first, last = _period(start, end)
    rows, accounts = costs.cost_rows(db, actor, start=first, end=last)
    context = {
        "shell": _shell(db, actor, "Cost per vehicle"),
        "rows": rows,
        "accounts": accounts,
        "totals": {a: sum((r.accounts.get(a, 0) for r in rows), costs.ZERO) for a in accounts},
        "grand": sum((r.total for r in rows), costs.ZERO),
        "start": first,
        "end": last,
        "page_title": "Cost per vehicle",
    }
    return _render(request, "fleet/costs.html", context)


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    vehicles.check_manager(db, actor)
    row = common.settings(db)
    return {
        "shell": _shell(db, actor, "Settings"),
        "values": {
            "first_alert_days": str(row.first_alert_days),
            "second_alert_days": str(row.second_alert_days),
            "low_efficiency_pct": str(row.low_efficiency_pct),
        },
        "errors": {},
        "notice": None,
        "page_title": "Fleet settings",
        **extra,
    }


@router.get("/fleet/settings", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.settings")
def settings_page(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _settings_context(db, actor, notice=NOTICES.get(notice))
    return _render(request, "fleet/settings.html", context)


@router.post("/fleet/settings", response_class=HTMLResponse)
@requires("fleet.vehicle.manage")
@screen("fleet.settings")
def save_settings(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    try:
        with db.begin_nested():
            costs.save_settings(
                db,
                actor,
                first=f.get("first_alert_days", ""),
                second=f.get("second_alert_days", ""),
                efficiency=f.get("low_efficiency_pct", ""),
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _settings_context(db, actor, values=f, errors=_errors(exc))
        return _render(request, "fleet/settings.html", context, 422)
    return _see_other("/fleet/settings?notice=saved")
