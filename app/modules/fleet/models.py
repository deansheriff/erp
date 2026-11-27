"""Fleet (ADR 0033): vehicles and their documents, drivers and licences, service plans and
maintenance, fuel logs, incidents and bookings. Cost per vehicle lives on the ledger
(journal lines name the vehicle)."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

Money = Numeric(16, 2)
TYPES = ("car", "pickup", "van", "bus", "truck", "motorcycle")
FUELS = ("petrol", "diesel", "cng", "electric")
STATUSES = ("active", "workshop", "retired")
DOCUMENT_KINDS = ("insurance", "licence", "roadworthiness", "other")
CLAIM_STATUSES = ("none", "filed", "approved", "rejected", "paid")
INCIDENT_STATUSES = ("reported", "reviewed", "closed")
BOOKING_STATUSES = ("requested", "approved", "rejected", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Vehicle(IdMixin, Base):
    __tablename__ = "vehicles"
    __table_args__ = (
        CheckConstraint(_in("type", TYPES), name="type"),
        CheckConstraint(_in("fuel", FUELS), name="fuel"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        CheckConstraint("start_odometer >= 0", name="odometer"),
    )

    code: Mapped[str] = mapped_column(String(20), unique=True)
    registration: Mapped[str] = mapped_column(String(20), unique=True)
    make: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(60))
    year: Mapped[int | None] = mapped_column(SmallInteger)
    type: Mapped[str] = mapped_column(String(10))
    fuel: Mapped[str] = mapped_column(String(8))
    colour: Mapped[str | None] = mapped_column(String(30))
    vin: Mapped[str | None] = mapped_column(String(40))
    purchase_date: Mapped[date | None]
    start_odometer: Mapped[int] = mapped_column(Integer, server_default="0")
    is_pool: Mapped[bool] = mapped_column(Boolean, server_default=false())
    driver_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    status: Mapped[str] = mapped_column(String(8), server_default="active")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VehicleDocument(IdMixin, Base):
    __tablename__ = "vehicle_documents"
    __table_args__ = (
        CheckConstraint(_in("kind", DOCUMENT_KINDS), name="kind"),
        CheckConstraint("issued_on IS NULL OR expires_on >= issued_on", name="dates"),
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(14))
    number: Mapped[str | None] = mapped_column(String(60))
    issuer: Mapped[str | None] = mapped_column(String(120))
    issued_on: Mapped[date | None]
    expires_on: Mapped[date]
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Driver(IdMixin, Base):
    __tablename__ = "fleet_drivers"

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), unique=True)
    licence_number: Mapped[str] = mapped_column(String(40))
    licence_class: Mapped[str | None] = mapped_column(String(10))
    licence_expiry: Mapped[date]
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())


class ServicePlan(IdMixin, Base):
    """A repeating service: every so many km, months, or both (whichever comes first)."""

    __tablename__ = "service_plans"
    __table_args__ = (
        CheckConstraint("every_km IS NOT NULL OR every_months IS NOT NULL", name="interval"),
        CheckConstraint("every_km IS NULL OR every_km > 0", name="km"),
        CheckConstraint("every_months IS NULL OR every_months > 0", name="months"),
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    every_km: Mapped[int | None] = mapped_column(Integer)
    every_months: Mapped[int | None] = mapped_column(SmallInteger)
    last_done_on: Mapped[date | None]
    last_done_km: Mapped[int | None] = mapped_column(Integer)
    alerted_for: Mapped[date | None]  # the last-done date an alert was sent for
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())


class Maintenance(IdMixin, Base):
    __tablename__ = "vehicle_maintenance"
    __table_args__ = (CheckConstraint("cost IS NULL OR cost >= 0", name="cost"),)

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("service_plans.id", ondelete="SET NULL")
    )
    day: Mapped[date]
    odometer: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(300))
    vendor: Mapped[str | None] = mapped_column(String(120))
    cost: Mapped[Decimal | None] = mapped_column(Money)
    reference: Mapped[str | None] = mapped_column(String(40))  # the bill or claim number
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FuelLog(IdMixin, Base):
    __tablename__ = "fuel_logs"
    __table_args__ = (
        CheckConstraint("litres > 0 AND amount >= 0 AND odometer >= 0", name="amounts"),
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vehicles.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    day: Mapped[date]
    litres: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    amount: Mapped[Decimal] = mapped_column(Money)
    odometer: Mapped[int] = mapped_column(Integer)
    station: Mapped[str | None] = mapped_column(String(120))
    reference: Mapped[str | None] = mapped_column(String(40))
    receipt_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    km_per_litre: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    flagged: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Incident(IdMixin, Base):
    __tablename__ = "vehicle_incidents"
    __table_args__ = (
        CheckConstraint(_in("status", INCIDENT_STATUSES), name="status"),
        CheckConstraint(_in("claim_status", CLAIM_STATUSES), name="claim"),
    )

    number: Mapped[str] = mapped_column(String(20), unique=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id"), index=True)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    occurred_at: Mapped[datetime]
    place: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    injuries: Mapped[str | None] = mapped_column(Text)
    police_report: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(8), server_default="reported")
    repair_cost: Mapped[Decimal | None] = mapped_column(Money)
    claim_number: Mapped[str | None] = mapped_column(String(60))
    claim_status: Mapped[str] = mapped_column(String(8), server_default="none")
    recovered: Mapped[Decimal | None] = mapped_column(Money)
    review_notes: Mapped[str | None] = mapped_column(Text)
    reported_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reported_at: Mapped[datetime] = mapped_column(server_default=func.now())
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None]


class IncidentPhoto(IdMixin, Base):
    __tablename__ = "vehicle_incident_photos"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vehicle_incidents.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id"))
    name: Mapped[str] = mapped_column(String(255))


class Booking(IdMixin, Base):
    __tablename__ = "vehicle_bookings"
    __table_args__ = (
        CheckConstraint(_in("status", BOOKING_STATUSES), name="status"),
        CheckConstraint("ends_at > starts_at", name="times"),
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id"), index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    purpose: Mapped[str] = mapped_column(String(200))
    destination: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(9), server_default="requested")
    driver_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employees.id"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FleetSettings(Base):
    """One row (id 1): alert lead times and the low-efficiency level (confirm)."""

    __tablename__ = "fleet_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="one_row"),
        CheckConstraint(
            "first_alert_days > second_alert_days AND second_alert_days > 0", name="alerts"
        ),
        CheckConstraint("low_efficiency_pct BETWEEN 1 AND 99", name="efficiency"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    first_alert_days: Mapped[int] = mapped_column(SmallInteger, server_default="30")
    second_alert_days: Mapped[int] = mapped_column(SmallInteger, server_default="7")
    low_efficiency_pct: Mapped[int] = mapped_column(SmallInteger, server_default="70")


class ExpiryAlert(IdMixin, Base):
    """An expiry alert already sent: for a document or a licence, at a number of days
    before (0 on the day), for a given expiry date."""

    __tablename__ = "fleet_expiry_alerts"
    __table_args__ = (UniqueConstraint("subject_id", "expires_on", "days_before"),)

    subject_id: Mapped[uuid.UUID]  # a vehicle document or a driver
    expires_on: Mapped[date]
    days_before: Mapped[int] = mapped_column(SmallInteger)
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now())
