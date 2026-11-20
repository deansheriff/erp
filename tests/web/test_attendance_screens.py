"""Attendance through the browser's eyes: HR sets a shift and a branch geofence, an
employee clocks in and out and asks for a correction, the approver sees it on Team today,
and the monthly summary downloads as CSV for payroll."""

import csv
import html
import io
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.attendance.models import AttendanceCorrection, AttendanceRecord, ShiftPattern
from app.modules.hr.models import Employee
from app.org.models import Branch
from tests.conftest import Installed, csrf_token, sign_in
from tests.web.test_hr_screens import _add_and_invite, _post

pytestmark = pytest.mark.db

PAGES = (
    "/attendance",
    "/attendance/timesheet",
    "/attendance/team",
    "/attendance/summary",
    "/attendance/roster",
    "/attendance/settings",
)


@pytest.fixture()
def admin(installed: Installed, client: TestClient) -> TestClient:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    return client


def _clock(client: TestClient, action: str, **fields: str) -> object:
    """Posts the clock form as the browser does: multipart, CSRF in the header."""
    return client.post(
        "/attendance/clock",
        data={"action": action, **fields},
        files={"file": ("", b"", "application/octet-stream")},
        headers={"X-CSRF-Token": csrf_token(client)},
    )


def _day_shift() -> uuid.UUID:
    with session_scope() as db:
        return db.scalars(select(ShiftPattern.id).where(ShiftPattern.name == "Day shift")).one()


def test_every_attendance_page_renders_for_the_super_admin(admin: TestClient) -> None:
    for page in PAGES:
        r = admin.get(page)
        assert r.status_code == 200, page
    assert "No HR record yet" in admin.get("/attendance").text
    nav = admin.get("/attendance/settings").text
    for href in PAGES[1:]:
        assert f'href="{href}"' in nav


def test_clock_in_out_correct_and_approve(admin: TestClient) -> None:
    person, amaka = _add_and_invite(admin)
    r = _post(
        admin,
        "/attendance/roster/assignments",
        employee_id=str(person),
        pattern_id=str(_day_shift()),
        start_date="2026-01-05",
    )
    assert r.status_code == 200 and "Day shift" in r.text  # type: ignore[attr-defined]

    page = amaka.get("/attendance")
    assert page.status_code == 200 and "Clock in" in page.text
    # The menu shows Amaka only her own pages.
    assert 'href="/attendance/timesheet"' in page.text
    assert 'href="/attendance/settings"' not in page.text
    assert 'href="/attendance/team"' not in page.text
    assert (
        _clock(amaka, "in", latitude="6.4281", longitude="3.4219", accuracy="20").status_code == 200
    )  # type: ignore[attr-defined]
    page = amaka.get("/attendance").text
    assert "Clock out" in page and "In since" in page
    r = _clock(amaka, "in")
    assert r.status_code == 422 and "already clocked in" in r.text  # type: ignore[attr-defined]
    assert _clock(amaka, "out").status_code == 200  # type: ignore[attr-defined]
    with session_scope() as db:
        record = db.scalars(
            select(AttendanceRecord).where(AttendanceRecord.employee_id == person)
        ).one()
        assert record.clock_out_at is not None and record.in_accuracy_m == 20
        day = record.work_date

    assert amaka.get("/attendance/settings").status_code == 403
    yesterday = day - timedelta(days=1)  # wholly in the past, whatever the time now
    r = _post(
        amaka,
        "/attendance/corrections",
        work_date=yesterday.isoformat(),
        clock_in="08:00",
        clock_out="17:00",
        reason="Started early, forgot",
    )
    assert r.status_code == 200 and "Correction sent" in r.text  # type: ignore[attr-defined]

    team = admin.get("/attendance/team").text
    assert "Amaka Okafor" in team and "Started early, forgot" in team
    with session_scope() as db:
        correction = db.scalars(
            select(AttendanceCorrection.id).where(AttendanceCorrection.employee_id == person)
        ).one()
    r = _post(admin, f"/attendance/corrections/{correction}/reject")
    assert r.status_code == 422 and "Say why" in html.unescape(r.text)  # type: ignore[attr-defined]
    r = _post(admin, f"/attendance/corrections/{correction}/approve")
    assert r.status_code == 200 and "Approved." in r.text  # type: ignore[attr-defined]
    assert "Corrected" in amaka.get(f"/attendance/timesheet?month={yesterday:%Y-%m}").text

    report = admin.get(f"/attendance/summary.csv?month={yesterday:%Y-%m}")
    assert report.status_code == 200 and report.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(report.text)))
    assert rows[0][:3] == ["Employee number", "Name", "Scheduled days"]
    assert any(r[1] == "Amaka Okafor" for r in rows[1:])
    assert "Amaka Okafor" in admin.get(f"/attendance/summary?month={yesterday:%Y-%m}").text


def test_geofenced_branch_refuses_a_clock_in_without_location(admin: TestClient) -> None:
    person, amaka = _add_and_invite(admin)
    with session_scope() as db:
        branch = Branch(code="LOS", name="Lagos Head Office")
        db.add(branch)
        db.flush()
        employee = db.get(Employee, person)
        assert employee is not None
        employee.branch_id = branch.id
        branch_id = branch.id
    r = _post(
        admin,
        f"/attendance/settings/locations/{branch_id}",
        geofence_enabled="1",
        latitude="",
        longitude="",
        radius_m="150",
    )
    assert r.status_code == 422 and "turn the geofence on" in r.text  # type: ignore[attr-defined]
    r = _post(
        admin,
        f"/attendance/settings/locations/{branch_id}",
        geofence_enabled="1",
        latitude="6.4281",
        longitude="3.4219",
        radius_m="150",
    )
    assert r.status_code == 200 and "Geofence on" in r.text  # type: ignore[attr-defined]
    page = amaka.get("/attendance").text
    assert 'data-required="true"' in page and "only clock in on site" in page
    r = _clock(amaka, "in")
    assert r.status_code == 422 and "Allow location" in r.text  # type: ignore[attr-defined]
    r = _clock(amaka, "in", latitude="6.5000", longitude="3.4219")
    assert r.status_code == 422 and "km from Lagos Head Office" in r.text  # type: ignore[attr-defined]
    assert _clock(amaka, "in", latitude="6.4282", longitude="3.4219").status_code == 200  # type: ignore[attr-defined]


def test_hr_edits_shift_patterns(admin: TestClient) -> None:
    r = _post(
        admin, "/attendance/settings/patterns", name="Weekend", start_time="09:00", end_time="15:00"
    )
    assert r.status_code == 422 and "Choose the days" in r.text  # type: ignore[attr-defined]
    r = admin.post(
        "/attendance/settings/patterns",
        data={
            "name": "Night shift",
            "start_time": "20:00",
            "end_time": "06:00",
            "break_minutes": "30",
            "weekdays": ["0", "1", "2", "3", "4"],
            "late_grace": "10",
            "early_grace": "10",
            "csrf_token": csrf_token(admin),
        },
    )
    assert r.status_code == 200 and "Night shift" in r.text and "Ends next day" in r.text
    roster = admin.get("/attendance/roster").text
    assert "Night shift 20:00" in roster
