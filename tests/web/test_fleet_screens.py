"""Fleet through the browser's eyes (Phase 11f): the administrator adds Amaka as a driver
and a pool vehicle assigned to her with its insurance and a service plan; Amaka logs
fuel with a receipt, books the vehicle and reports an incident; the administrator
approves the booking, reviews and closes the incident; and every fleet page renders."""

import html
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import clock
from app.core.db import session_scope
from app.modules.fleet.models import Booking, FuelLog, Incident, ServicePlan, Vehicle
from tests.conftest import csrf_token
from tests.web.test_expense_screens import JPEG
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)
from tests.web.test_hr_screens import _add_and_invite

pytestmark = pytest.mark.db


def _post(client: TestClient, url: str, data: dict[str, Any] | None = None) -> Any:
    return client.post(url, data={**(data or {}), "csrf_token": csrf_token(client)})


def _upload(client: TestClient, url: str, field: str, data: dict[str, str]) -> Any:
    return client.post(
        url,
        data=data,
        files={field: ("scan.jpg", JPEG, "image/jpeg")},
        headers={"X-CSRF-Token": csrf_token(client)},
    )


def _one(model: type, **where: object) -> str:
    with session_scope() as db:
        query = select(model.id)  # type: ignore[attr-defined]
        for key, value in where.items():
            query = query.where(getattr(model, key) == value)
        return str(db.scalars(query).one())


def test_every_fleet_page_renders(admin: TestClient) -> None:  # noqa: F811
    for page in (
        "/fleet",
        "/fleet/vehicles",
        "/fleet/vehicles?status=retired&pool=1",
        "/fleet/vehicles/new",
        "/fleet/bookings",
        "/fleet/incidents",
        "/fleet/drivers",
        "/fleet/costs",
        "/fleet/costs?start=2026-01-01&end=2026-06-30",
        "/fleet/settings",
    ):
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/fleet/vehicles").text
    for href in ("/fleet/bookings", "/fleet/drivers", "/fleet/costs", "/fleet/settings"):
        assert f'href="{href}"' in nav
    r = _post(
        admin,
        "/fleet/settings",
        {"first_alert_days": "7", "second_alert_days": "30", "low_efficiency_pct": "70"},
    )
    assert r.status_code == 422 and "must come later" in html.unescape(r.text)
    r = _post(admin, "/fleet/vehicles/new", {"registration": "", "type": "car", "fuel": "petrol"})
    assert r.status_code == 422


def test_a_vehicle_from_register_to_incident(admin: TestClient) -> None:  # noqa: F811
    today = clock.today()
    person, amaka = _add_and_invite(admin)
    assert amaka.get("/fleet/vehicles").status_code == 403
    r = _post(
        admin,
        "/fleet/drivers",
        {
            "employee_id": str(person),
            "licence_number": "LAG-55120",
            "licence_class": "B",
            "licence_expiry": (today + timedelta(days=500)).isoformat(),
            "active": "1",
        },
    )
    assert r.status_code == 200 and "Driver saved." in r.text and "LAG-55120" in r.text
    r = _post(
        admin,
        "/fleet/vehicles/new",
        {
            "registration": "KJA 233 AB",
            "make": "Toyota",
            "model": "Corolla",
            "year": "2020",
            "type": "car",
            "fuel": "petrol",
            "start_odometer": "40,000",
            "is_pool": "1",
            "driver_id": str(person),
        },
    )
    assert r.status_code == 200 and "VEH-00001" in r.text
    vehicle = _one(Vehicle, registration="KJA 233 AB")

    r = _upload(
        admin,
        f"/fleet/vehicles/{vehicle}/documents",
        "file",
        {
            "kind": "insurance",
            "number": "AXA-7781",
            "issuer": "AXA Mansard",
            "expires_on": (today + timedelta(days=20)).isoformat(),
        },
    )
    assert r.status_code == 200 and "Document added." in r.text and "20 days left" in r.text
    assert "Papers to renew" in admin.get("/fleet/vehicles").text
    r = _post(
        admin,
        f"/fleet/vehicles/{vehicle}/plans",
        {"plan_name": "Oil change", "every_km": "5,000", "every_months": "6"},
    )
    assert r.status_code == 200 and "45,000 km" in r.text
    r = _post(
        admin,
        f"/fleet/vehicles/{vehicle}/services",
        {
            "plan_id": _one(ServicePlan, name="Oil change"),
            "service_day": today.isoformat(),
            "service_odometer": "40,200",
            "vendor": "Coscharis",
        },
    )
    assert r.status_code == 200 and "45,200 km" in r.text

    page = html.unescape(amaka.get("/fleet").text)
    assert "KJA 233 AB" in page and "40,200 km" in page
    r = _upload(
        amaka,
        "/fleet/fuel",
        "receipt",
        {
            "vehicle_id": vehicle,
            "fuel_day": today.isoformat(),
            "litres": "30",
            "fuel_amount": "27,000",
            "fuel_odometer": "40,100",
        },
    )
    assert r.status_code == 422 and "40,200 km" in html.unescape(r.text)
    r = _upload(
        amaka,
        "/fleet/fuel",
        "receipt",
        {
            "vehicle_id": vehicle,
            "fuel_day": today.isoformat(),
            "litres": "30",
            "fuel_amount": "27,000",
            "fuel_odometer": "40,500",
            "station": "Total, Ikeja",
        },
    )
    assert r.status_code == 200 and "Fuel logged." in r.text
    with session_scope() as db:
        assert db.scalars(select(FuelLog.receipt_file_id)).one() is not None

    start = today + timedelta(days=2)
    r = _post(
        amaka,
        "/fleet/bookings/new",
        {
            "booking_vehicle_id": vehicle,
            "starts_at": f"{start.isoformat()}T09:00",
            "ends_at": f"{start.isoformat()}T13:00",
            "purpose": "Client visit",
            "destination": "Victoria Island",
        },
    )
    assert r.status_code == 200 and "Booking requested." in r.text
    booking = _one(Booking, purpose="Client visit")
    assert "Client visit" in admin.get("/fleet/bookings").text
    r = _post(admin, f"/fleet/bookings/{booking}/decide", {"decision": "reject"})
    assert r.status_code == 422 and "Say why" in html.unescape(r.text)
    r = _post(admin, f"/fleet/bookings/{booking}/decide", {"decision": "approve"})
    assert r.status_code == 200 and "Booking approved." in r.text
    assert "Approved" in amaka.get("/fleet").text

    r = _post(
        amaka,
        "/fleet/incidents/new",
        {
            "incident_vehicle_id": vehicle,
            "occurred_at": f"{today.isoformat()}T07:30",
            "place": "Ikorodu Road",
            "description": "A bus scraped the rear door.",
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "Incident reported." in page and "INC-00001" in page
    incident = _one(Incident, number="INC-00001")
    r = _upload(amaka, f"/fleet/incidents/{incident}/photos", "photo", {})
    assert r.status_code == 200 and "Photo added." in r.text
    assert "INC-00001" in amaka.get("/fleet/incidents").text
    r = _post(
        admin,
        f"/fleet/incidents/{incident}/review",
        {
            "repair_cost": "85,000",
            "claim_status": "paid",
            "claim_number": "AXA-C-301",
            "recovered": "60,000",
            "then": "close",
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "Incident closed." in page and "₦60,000.00" in page
    assert _post(amaka, f"/fleet/incidents/{incident}/review", {}).status_code == 403
    assert admin.get(f"/fleet/vehicles/{vehicle}").status_code == 200
    assert "KJA 233 AB" in admin.get("/fleet/costs").text
