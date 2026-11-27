"""Phase 11f: vehicles with documents and drivers, fuel logs with odometer and efficiency
checks, service plans by distance or time, expiry and service alerts sent once,
bookings approved by a Fleet Manager, incidents with claims, and cost per vehicle from
the ledger (ADR 0033)."""

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.mail.models import MailOutbox
from app.modules.finance import bills, suppliers
from app.modules.finance.bills import BillInput, BillLineInput
from app.modules.finance.models import JournalLine, WhtType
from app.modules.finance.suppliers import SupplierInput
from app.modules.fleet import alerts, bookings, costs, fuel, incidents, vehicles
from app.modules.fleet.bookings import BookingInput
from app.modules.fleet.fuel import FuelInput
from app.modules.fleet.incidents import ReportInput, ReviewInput
from app.modules.fleet.models import FuelLog, Vehicle
from app.modules.fleet.vehicles import DocumentInput, DriverInput, ServiceInput
from tests.fleet.conftest import Yard

pytestmark = pytest.mark.db

D = Decimal
LAGOS = ZoneInfo("Africa/Lagos")


def _fuel(y: Yard, who: str, vehicle: uuid.UUID, litres: str, odometer: str) -> uuid.UUID:
    return fuel.log(
        y.db,
        y.actor(who),
        FuelInput(str(vehicle), clock.today(), litres, "45,000", odometer, "Total, Ikeja", ""),
    )


def _doc(y: Yard, vehicle: uuid.UUID, kind: str, days: int) -> None:
    vehicles.add_document(
        y.db,
        y.actor("Chi"),
        vehicle,
        DocumentInput(kind, "P-77", "AXA Mansard", None, clock.today() + timedelta(days=days)),
    )


def _slot(days: int, start: int, end: int) -> tuple[datetime, datetime]:
    day = clock.today() + timedelta(days=days)
    return (
        datetime.combine(day, time(start), LAGOS),
        datetime.combine(day, time(end), LAGOS),
    )


def test_vehicles_drivers_and_documents(yard: Yard) -> None:
    y, a = yard, yard.actor
    with pytest.raises(PermissionDenied):
        vehicles.save(
            y.db, a("Dayo"), None, vehicles.VehicleInput(*([""] * 8), None, "", False, "", "")
        )
    with pytest.raises(ValidationFailed) as unlicensed:
        y.vehicle(driver="Hauwa")
    assert "driver_id" in (unlicensed.value.field_errors or {})
    hilux = y.vehicle(driver="Dayo")
    row = y.db.get(Vehicle, hilux)
    assert row is not None and row.code == "VEH-00001" and row.registration == "LSD 482 KJ"
    with pytest.raises(ValidationFailed):
        y.vehicle()  # the same registration

    _doc(y, hilux, "insurance", 20)
    _doc(y, hilux, "roadworthiness", -1)
    listed = vehicles.list_vehicles(y.db, a("Chi"))[0]
    assert listed.documents == "expired" and "Roadworthiness" in (listed.document_note or "")

    vehicles.save_driver(
        y.db,
        a("Chi"),
        DriverInput(str(y.employee("Efe")), "LAG-9", "B", clock.today() - timedelta(days=1), True),
    )
    rows = {r.name: r.state for r in vehicles.driver_rows(y.db, a("Chi"))}
    assert sorted(rows.values()) == ["expired", "valid"]


def test_fuel_logs_check_the_odometer_and_flag_poor_efficiency(yard: Yard) -> None:
    y, a = yard, yard.actor
    hilux = y.vehicle(driver="Dayo")
    with pytest.raises(PermissionDenied):
        _fuel(y, "Hauwa", hilux, "50", "1200")  # not the driver
    with pytest.raises(ValidationFailed):
        _fuel(y, "Dayo", hilux, "50", "900")  # below the starting odometer
    _fuel(y, "Dayo", hilux, "40", "1200")
    _fuel(y, "Dayo", hilux, "50", "1700")  # 500 km on 50 L: 10 km/L
    _fuel(y, "Chi", hilux, "40", "1900")  # 200 km on 40 L: 5 km/L, under 70% of the average
    logs = list(y.db.scalars(select(FuelLog).order_by(FuelLog.odometer)))
    assert [(x.km_per_litre, x.flagged) for x in logs] == [
        (None, False),
        (D("10.00"), False),
        (D("5.00"), True),
    ]
    with pytest.raises(ValidationFailed) as back:
        _fuel(y, "Dayo", hilux, "10", "1800")
    assert "1,900 km" in (back.value.field_errors or {})["fuel_odometer"]
    assert [v.id for v in fuel.my_vehicles(y.db, a("Dayo"))] == [hilux]


def test_service_plans_and_alerts_are_sent_once(yard: Yard) -> None:
    y, a = yard, yard.actor
    hilux = y.vehicle(driver="Dayo")
    vehicles.save_plan(y.db, a("Chi"), hilux, name="Oil change", every_km="5000", every_months="3")
    view = vehicles.get(y.db, a("Chi"), hilux)
    assert view.plans[0].due_km == 6000 and not view.plans[0].overdue
    _fuel(y, "Dayo", hilux, "60", "6100")
    _doc(y, hilux, "insurance", 20)
    _doc(y, hilux, "licence", 5)
    sent = alerts.run(y.db, clock.today(), "Adeyemi Holdings")
    assert (sent.expiries, sent.services) == (2, 1)  # insurance inside 30 days, licence inside 7
    again = alerts.run(y.db, clock.today(), "Adeyemi Holdings")
    assert (again.expiries, again.services) == (0, 0)
    vehicles.record_service(
        y.db,
        a("Chi"),
        hilux,
        ServiceInput(
            str(view.plans[0].plan.id),
            clock.today(),
            "6100",
            "",
            "Coscharis",
            "35,000",
            "BILL-00009",
        ),
    )
    view = vehicles.get(y.db, a("Chi"), hilux)
    assert view.plans[0].due_km == 11100 and view.services[0].description == "Oil change"
    mails = list(y.db.scalars(select(MailOutbox).where(MailOutbox.template.like("fleet_%"))))
    assert {m.template for m in mails} == {"fleet_expiry_alert", "fleet_service_due"}


def test_bookings_are_approved_by_a_fleet_manager(yard: Yard) -> None:
    y, a = yard, yard.actor
    corolla = y.vehicle("KJA 233 AB", pool=True)
    _doc(y, corolla, "roadworthiness", -2)
    start, end = _slot(1, 9, 12)
    with pytest.raises(ValidationFailed) as expired:
        bookings.request(
            y.db, a("Hauwa"), BookingInput(str(corolla), start, end, "Client visit", "")
        )
    assert "expired" in (expired.value.field_errors or {})["vehicle_id"]
    _doc(y, corolla, "roadworthiness", 300)
    first = bookings.request(
        y.db, a("Hauwa"), BookingInput(str(corolla), start, end, "Client visit", "Ikeja")
    )
    second = bookings.request(
        y.db, a("Efe"), BookingInput(str(corolla), *_slot(1, 11, 14), "Bank run", "")
    )
    with pytest.raises(PermissionDenied):
        bookings.decide(y.db, a("Hauwa"), first, approve=True)
    with pytest.raises(ValidationFailed):
        bookings.decide(y.db, a("Chi"), first, approve=True, driver_id=str(y.employee("Hauwa")))
    bookings.decide(y.db, a("Chi"), first, approve=True, driver_id=str(y.employee("Dayo")))
    with pytest.raises(Conflict, match="overlaps"):
        bookings.decide(y.db, a("Chi"), second, approve=True)
    with pytest.raises(ValidationFailed):
        bookings.decide(y.db, a("Chi"), second, approve=False)
    bookings.decide(y.db, a("Chi"), second, approve=False, reason="Already out that morning")
    assert [r.booking.id for r in bookings.upcoming(y.db, a("Chi"))] == [first]
    assert [r.booking.status for r in bookings.mine(y.db, a("Efe"))] == ["rejected"]


def test_incidents_are_reviewed_and_closed(yard: Yard) -> None:
    y, a = yard, yard.actor
    hilux = y.vehicle(driver="Dayo")
    with pytest.raises(ValidationFailed):
        incidents.report(y.db, a("Dayo"), ReportInput(str(hilux), None, "", "", "", ""))
    incident = incidents.report(
        y.db,
        a("Dayo"),
        ReportInput(
            str(hilux),
            clock.now() - timedelta(hours=2),
            "Third Mainland Bridge",
            "Rear-ended in traffic",
            "None",
            "LP/221/26",
        ),
    )
    assert incidents.get(y.db, a("Dayo"), incident).incident.number == "INC-00001"
    with pytest.raises(NotFound):
        incidents.get(y.db, a("Hauwa"), incident)
    with pytest.raises(PermissionDenied):
        incidents.review(
            y.db, a("Dayo"), incident, ReviewInput("", "", "none", "", ""), close=False
        )
    with pytest.raises(ValidationFailed):
        incidents.review(
            y.db, a("Chi"), incident, ReviewInput("150,000", "", "filed", "", ""), close=False
        )
    incidents.review(
        y.db,
        a("Chi"),
        incident,
        ReviewInput("150,000", "CLM-88", "paid", "120,000", ""),
        close=True,
    )
    view = incidents.get(y.db, a("Chi"), incident)
    assert (view.incident.status, view.incident.recovered) == ("closed", D("120000.00"))


def test_cost_per_vehicle_comes_from_the_ledger(yard: Yard) -> None:
    y, a = yard, yard.actor
    hilux = y.vehicle(driver="Dayo")
    _fuel(y, "Dayo", hilux, "40", "1200")
    _fuel(y, "Dayo", hilux, "50", "1700")
    wht = y.db.scalars(select(WhtType).where(WhtType.key == "supply_of_goods")).one()
    supplier = suppliers.save(
        y.db,
        a("Bola"),
        None,
        SupplierInput(
            "Coscharis Motors", "company", "", "", "", "", "30", str(wht.id), "", "", "", "", True
        ),
    )
    bill = bills.save_bill(
        y.db,
        a("Bola"),
        None,
        BillInput(
            "bill",
            str(supplier),
            "CM-100",
            clock.today(),
            None,
            "",
            "",
            [
                BillLineInput(
                    "",
                    "Brake pads",
                    str(y.account("6300").id),
                    "",
                    "1",
                    "100,000",
                    "exempt",
                    vehicle_id=str(hilux),
                )
            ],
        ),
    )
    bills.approve(y.db, a("Gbenga"), bill)
    assert y.db.scalars(select(JournalLine).where(JournalLine.vehicle_id == hilux)).one()
    rows, accounts = costs.cost_rows(y.db, a("Bola"), start=None, end=None)
    row = next(r for r in rows if r.vehicle.id == hilux)
    assert (row.total, row.distance, row.per_km) == (D("100000.00"), 500, D("200.00"))
    assert len(accounts) == 1
    with pytest.raises(PermissionDenied):
        costs.cost_rows(y.db, a("Dayo"), start=None, end=None)
    with pytest.raises(ValidationFailed):
        costs.save_settings(y.db, a("Chi"), first="7", second="30", efficiency="70")
