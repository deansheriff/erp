"""Leave through the browser's eyes: an employee books leave, their manager approves it in
the inbox, the team calendar and balances show it, and HR edits settings."""

import html
import re
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import clock
from app.core.db import session_scope
from app.modules.leave.models import LeaveRequest, LeaveType, PublicHoliday
from tests.conftest import Installed, sign_in
from tests.web.test_hr_screens import _add_and_invite, _post

pytestmark = pytest.mark.db

YEAR = clock.today().year + 1
MON = date(YEAR, 3, 2) - timedelta(days=date(YEAR, 3, 2).weekday())


@pytest.fixture()
def admin(installed: Installed, client: TestClient) -> TestClient:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    return client


def _type_id(code: str) -> uuid.UUID:
    with session_scope() as db:
        return db.scalars(select(LeaveType.id).where(LeaveType.code == code)).one()


def test_every_leave_page_renders_for_the_super_admin(admin: TestClient) -> None:
    for page in (
        "/leave",
        "/leave/approvals",
        "/leave/calendar",
        "/leave/balances",
        "/leave/settings",
    ):
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/leave/settings").text
    for href in ("/leave/approvals", "/leave/calendar", "/leave/balances", "/leave/settings"):
        assert f'href="{href}"' in nav


def test_book_check_and_approve(admin: TestClient) -> None:
    _, amaka = _add_and_invite(admin)  # hire date 2025-02-03, no manager: HR approves
    page = amaka.get("/leave").text
    assert "Annual leave" in page and "Request leave" in page
    form = {
        "leave_type_id": str(_type_id("annual")),
        "start_date": MON.isoformat(),
        "end_date": (MON + timedelta(days=2)).isoformat(),
    }
    r = _post(amaka, "/leave/new", check="1", **form)
    assert r.status_code == 200 and "3 days" in html.unescape(r.text)  # type: ignore[attr-defined]
    with session_scope() as db:
        assert db.scalars(select(LeaveRequest)).first() is None  # checking saves nothing
    r = _post(amaka, "/leave/new", **form)
    assert r.status_code == 200 and "Request sent." in r.text  # type: ignore[attr-defined]
    assert amaka.get("/leave/settings").status_code == 403
    assert amaka.get("/leave/approvals").status_code == 403
    own = amaka.get("/leave/balances").text  # self scope: only her own balances
    assert "Amaka Okafor" in own and "Save adjustment" not in own

    inbox = admin.get("/leave/approvals").text
    assert "Amaka Okafor" in inbox
    request_id = re.search(r"/leave/requests/([0-9a-f-]{36})/approve", inbox)
    assert request_id
    r = _post(admin, f"/leave/requests/{request_id.group(1)}/reject")
    assert r.status_code == 422  # type: ignore[attr-defined]  # a note is required
    r = _post(admin, f"/leave/requests/{request_id.group(1)}/approve")
    assert r.status_code == 200 and "Approved." in r.text  # type: ignore[attr-defined]
    assert "Approved" in amaka.get("/leave").text
    calendar = admin.get(f"/leave/calendar?month={MON:%Y-%m}").text
    assert "Amaka Okafor" in calendar
    balances = html.unescape(admin.get(f"/leave/balances?year={YEAR}&q=amaka").text)
    assert "Amaka Okafor" in balances


def test_errors_show_on_the_form(admin: TestClient) -> None:
    _, amaka = _add_and_invite(admin)
    r = _post(
        amaka,
        "/leave/new",
        leave_type_id=str(_type_id("annual")),
        start_date=MON.isoformat(),
        end_date=(MON + timedelta(days=20)).isoformat(),
    )
    assert r.status_code == 422 and "are available" in html.unescape(r.text)  # type: ignore[attr-defined]


def test_hr_adjusts_balances_and_edits_settings(admin: TestClient) -> None:
    person, _ = _add_and_invite(admin)
    r = _post(
        admin,
        "/leave/balances/adjust",
        employee_id=str(person),
        leave_type_id=str(_type_id("annual")),
        year=str(YEAR),
        days_change="1.5",
        reason="Worked on a public holiday",
    )
    assert r.status_code == 200 and "Balance adjusted." in r.text  # type: ignore[attr-defined]
    assert "Worked on a public holiday" in r.text  # type: ignore[attr-defined]
    day = date(YEAR, 7, 15)
    r = _post(admin, "/leave/settings/holidays", day=day.isoformat(), holiday_name="Test Holiday")
    assert r.status_code == 200 and "Test Holiday" in r.text  # type: ignore[attr-defined]
    with session_scope() as db:
        holiday = db.scalars(select(PublicHoliday).where(PublicHoliday.day == day)).one()
    _post(admin, f"/leave/settings/holidays/{holiday.id}/delete")
    with session_scope() as db:
        assert db.get(PublicHoliday, holiday.id) is None
    r = _post(admin, "/leave/settings/types", name="", counting="working", accrual="yearly")
    assert r.status_code == 422  # type: ignore[attr-defined]
