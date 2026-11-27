"""The daily fleet alerts (ADR 0033): documents and licences expiring at the set lead
times and on the day (to the Fleet Managers, and the driver for their licence), and
service plans falling due (to the Fleet Managers). Each is sent once."""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import engine
from app.identity.models import User
from app.mail import service as mail
from app.modules.fleet import common, vehicles
from app.modules.fleet.models import Driver, ExpiryAlert, ServicePlan, Vehicle, VehicleDocument
from app.modules.hr import service as people


@dataclass(frozen=True, slots=True)
class Sent:
    expiries: int
    services: int


def _managers(db: Session) -> list[User]:
    return [
        u
        for u in db.scalars(select(User).where(User.status == "active").order_by(User.email))
        if engine.check(db, u.id, common.MANAGE)
    ]


def _level(expires: date, today: date, first: int, second: int) -> int | None:
    """The alert level due today: first or second lead time, or 0 on/after the day."""
    days = (expires - today).days
    if days <= 0:
        return 0
    if days <= second:
        return second
    if days <= first:
        return first
    return None


def _send_once(db: Session, subject_id: uuid.UUID, expires: date, level: int) -> bool:
    exists = db.scalar(
        select(ExpiryAlert.id).where(
            ExpiryAlert.subject_id == subject_id,
            ExpiryAlert.expires_on == expires,
            ExpiryAlert.days_before == level,
        )
    )
    if exists:
        return False
    db.add(ExpiryAlert(subject_id=subject_id, expires_on=expires, days_before=level))
    return True


def _mail(db: Session, users: list[User], organisation: str, what: str, expires: date) -> None:
    for user in users:
        mail.queue(
            db,
            to=user.email,
            template="fleet_expiry_alert",
            context={
                "organisation": organisation,
                "name": user.full_name,
                "what": what,
                "expires": f"{expires:%d %b %Y}",
            },
        )


def _expiries(db: Session, today: date, organisation: str, managers: list[User]) -> int:
    settings = common.settings(db)
    count = 0
    for vehicle in db.scalars(select(Vehicle).where(Vehicle.status != "retired")):
        for kind, doc in common.current_documents(db, vehicle.id).items():
            level = _level(
                doc.expires_on, today, settings.first_alert_days, settings.second_alert_days
            )
            if level is not None and _send_once(db, doc.id, doc.expires_on, level):
                what = f"The {common.DOCUMENT_LABELS[kind].lower()} for {common.label(vehicle)}"
                _mail(db, managers, organisation, what, doc.expires_on)
                count += 1
    for driver in db.scalars(select(Driver).where(Driver.is_active)):
        level = _level(
            driver.licence_expiry, today, settings.first_alert_days, settings.second_alert_days
        )
        if level is None or not _send_once(db, driver.id, driver.licence_expiry, level):
            continue
        name = people.names(db, {driver.employee_id}).get(driver.employee_id, "")
        account = people.accounts_of(db, {driver.employee_id}).get(driver.employee_id)
        own = db.get(User, account) if account else None
        targets = managers + (
            [own] if own and own.status == "active" and own not in managers else []
        )
        _mail(db, targets, organisation, f"{name}'s driving licence", driver.licence_expiry)
        count += 1
    return count


def _services(db: Session, organisation: str, managers: list[User]) -> int:
    count = 0
    for plan in db.scalars(select(ServicePlan).where(ServicePlan.is_active)):
        vehicle = db.get(Vehicle, plan.vehicle_id)
        if vehicle is None or vehicle.status == "retired":
            continue
        state = vehicles.plan_state(db, vehicle, plan, common.odometer(db, vehicle))
        marker = plan.last_done_on or vehicle.purchase_date or date(2000, 1, 1)
        if not state.overdue or plan.alerted_for == marker:
            continue
        plan.alerted_for = marker
        for user in managers:
            mail.queue(
                db,
                to=user.email,
                template="fleet_service_due",
                context={
                    "organisation": organisation,
                    "name": user.full_name,
                    "plan": plan.name,
                    "vehicle": common.label(vehicle),
                },
            )
        count += 1
    return count


def run(db: Session, today: date, organisation: str) -> Sent:
    managers = _managers(db)
    sent = Sent(_expiries(db, today, organisation, managers), _services(db, organisation, managers))
    db.flush()
    return sent


def documents_due(db: Session, today: date, within: int) -> list[tuple[Vehicle, VehicleDocument]]:
    """Current documents expiring within `within` days (or already expired)."""
    out = []
    for vehicle in db.scalars(select(Vehicle).where(Vehicle.status != "retired")):
        for doc in common.current_documents(db, vehicle.id).values():
            if (doc.expires_on - today).days <= within:
                out.append((vehicle, doc))
    return sorted(out, key=lambda pair: pair[1].expires_on)
