"""Phase 6 screens end to end: adding a person through the form, inviting them, their
self-service profile and requests, the directory, documents, exits and HR settings, and
that hidden classes never reach the page."""

import html
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.hr.models import ChecklistTask, Employee
from tests.conftest import Installed, csrf_token, sign_in
from tests.identity.test_accounts import _accept, _last_mail

pytestmark = pytest.mark.db

ADMIN_PAGES = [
    "/hr/people",
    "/hr/people/new",
    "/hr/change-requests",
    "/hr/checklists",
    "/hr/settings",
    "/directory",
    "/me",
]


@pytest.fixture()
def admin(installed: Installed, client: TestClient) -> TestClient:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    return client


def _post(client: TestClient, url: str, **data: str) -> object:
    return client.post(url, data={**data, "csrf_token": csrf_token(client)})


def _employee_id(email: str) -> uuid.UUID:
    with session_scope() as db:
        return db.scalars(select(Employee.id).where(Employee.work_email == email)).one()


def _add_and_invite(
    admin: TestClient, email: str = "amaka@adeyemiholdings.ng"
) -> tuple[uuid.UUID, TestClient]:
    r = _post(
        admin,
        "/hr/people/new",
        first_name="Amaka",
        last_name="Okafor",
        work_email=email,
        phone="0803 123 4567",
        hire_date="2025-02-03",
        nin="12345678901",
        account_number="0123456789",
        bank_name="GTBank",
    )
    assert r.status_code == 200, r.text[:500]  # type: ignore[attr-defined]
    person = _employee_id(email)
    assert _post(admin, f"/hr/people/{person}/invite").status_code == 200  # type: ignore[attr-defined]
    token = re.search(r"/invite/([A-Za-z0-9_-]+)", _last_mail("invite").body_text)
    assert token
    return person, _accept(token.group(1), admin.app)


def test_every_hr_page_renders_for_the_super_admin(admin: TestClient) -> None:
    for page in ADMIN_PAGES:
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/hr/people").text
    for href in ("/directory", "/hr/change-requests", "/hr/checklists", "/hr/settings"):
        assert f'href="{href}"' in nav


def test_adding_a_person_through_the_form(admin: TestClient) -> None:
    r = _post(admin, "/hr/people/new", first_name="Tunde", last_name="Bakare", nin="123")
    assert r.status_code == 422 and "The NIN is 11 digits." in r.text  # type: ignore[attr-defined]
    with session_scope() as db:  # the failed form saved nothing
        assert db.scalars(select(Employee).where(Employee.first_name == "Tunde")).first() is None
    r = _post(admin, "/hr/people/new", first_name="Tunde", last_name="Bakare", nin="12345678901")
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert "Tunde Bakare" in page and "EMP-00001" in page and "12345678901" in page


def test_the_invited_employee_sees_only_their_own_details(admin: TestClient) -> None:
    person, amaka = _add_and_invite(admin)
    me = html.unescape(amaka.get("/me").text)
    assert "Amaka Okafor" in me and "12345678901" in me and "0803 123 4567" in me
    # A colleague's profile shows directory fields only.
    _post(
        admin,
        "/hr/people/new",
        first_name="Femi",
        last_name="Ade",
        phone="0809 999 9999",
        work_email="femi@adeyemiholdings.ng",
        nin="98765432109",
    )
    femi = _employee_id("femi@adeyemiholdings.ng")
    profile = html.unescape(amaka.get(f"/hr/people/{femi}").text)
    assert "Femi Ade" in profile
    assert "0809 999 9999" not in profile and "98765432109" not in profile
    directory = html.unescape(amaka.get("/directory?q=femi").text)
    assert "femi@adeyemiholdings.ng" in directory and "0809 999 9999" not in directory
    # HR pages stay closed.
    for page in (
        "/hr/people/new",
        "/hr/settings",
        "/hr/change-requests",
        f"/hr/people/{femi}/edit",
    ):
        assert amaka.get(page).status_code == 403, page
    own = amaka.get(f"/hr/people/{person}/edit")  # only her self-service sections
    assert own.status_code == 200
    assert 'name="phone"' in own.text and 'name="next_of_kin_name"' in own.text
    for hidden in ('name="nin"', 'name="hire_date"', 'name="first_name"', 'name="bvn"'):
        assert hidden not in own.text


def test_self_service_edits_and_bank_change_requests(admin: TestClient) -> None:
    person, amaka = _add_and_invite(admin)
    r = _post(amaka, "/me/details", phone="0802 222 2222", next_of_kin_name="Chidi Okafor")
    assert r.status_code == 200 and "Your details are saved." in r.text  # type: ignore[attr-defined]
    # Fields outside self-service are refused even if posted.
    r = _post(amaka, "/me/details", hire_date="2010-01-01")
    assert r.status_code == 200  # ignored: not an editable field for them
    with session_scope() as db:
        row = db.get(Employee, person)
        assert row is not None and str(row.hire_date) == "2025-02-03"
        assert row.phone == "0802 222 2222"

    r = _post(
        amaka,
        "/me/requests/banking",
        bank_name="Access Bank",
        account_number="0987654321",
        account_name="Amaka Okafor",
        bvn="",
    )
    assert r.status_code == 200 and "Request sent to HR" in r.text  # type: ignore[attr-defined]
    inbox = html.unescape(admin.get("/hr/change-requests").text)
    assert "Amaka Okafor" in inbox and "0987654321" in inbox
    request_id = re.search(r"/hr/change-requests/([0-9a-f-]{36})/approve", inbox)
    assert request_id
    r = _post(admin, f"/hr/change-requests/{request_id.group(1)}/approve")
    assert r.status_code == 200 and "Approved and applied." in r.text  # type: ignore[attr-defined]
    assert "0987654321" in amaka.get("/me").text


def test_documents_upload_with_the_csrf_header(admin: TestClient) -> None:
    person, _ = _add_and_invite(admin)
    page = admin.get(f"/hr/people/{person}").text
    category = re.search(r'<option value="([0-9a-f-]{36})">Certificate', page)
    assert category
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    r = admin.post(
        f"/hr/people/{person}/documents",
        data={"category_id": category.group(1), "title": "BSc Accounting"},
        files={"file": ("bsc.pdf", pdf, "application/pdf")},
        headers={"X-CSRF-Token": csrf_token(admin)},
    )
    assert r.status_code == 200, r.text[:300]
    assert "BSc Accounting" in r.text and "Checking for viruses" in r.text


def test_exit_from_the_profile_starts_offboarding(admin: TestClient) -> None:
    person, amaka = _add_and_invite(admin)
    admin.post(
        "/hr/settings/templates",
        data={
            "csrf_token": csrf_token(admin),
            "kind": "offboarding",
            "name": "Leavers",
            "task_title": ["Return laptop"],
            "task_assignee": ["employee"],
            "task_due": ["0"],
        },
    )
    r = _post(
        admin,
        f"/hr/people/{person}/exit",
        kind="resignation",
        reason="Relocating",
        notice_date="2026-11-01",
        last_working_day="2099-12-31",
        final_pay_due="1",
    )
    assert r.status_code == 200, r.text[:300]  # type: ignore[attr-defined]
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert "Access ends automatically" in page and "Leaving" in page
    with session_scope() as db:
        tasks = db.scalars(select(ChecklistTask)).all()
    assert [t.title for t in tasks] == ["Return laptop"]
    board = admin.get("/hr/checklists?tab=offboarding").text
    assert "Amaka Okafor" in board
    me = amaka.get("/me?tab=tasks")  # still has access before the last day
    assert me.status_code == 200 and "Return laptop" in me.text


def test_hr_settings_forms(admin: TestClient) -> None:
    r = _post(admin, "/hr/settings/numbers", prefix="ah-", padding="4")
    assert r.status_code == 200 and "AH-0001" in r.text  # type: ignore[attr-defined]
    r = _post(admin, "/hr/settings/categories", name="Guarantor form", field_class="personal")
    assert "Guarantor form" in r.text  # type: ignore[attr-defined]
    r = _post(admin, "/hr/settings/categories", name="", field_class="personal")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = admin.post(
        "/hr/settings/templates",
        data={
            "csrf_token": csrf_token(admin),
            "kind": "onboarding",
            "name": "Drivers",
            "task_title": ["Licence check", "Vehicle handover", ""],
            "task_assignee": ["hr", "manager", "hr"],
            "task_due": ["0", "1", "0"],
        },
    )
    assert r.status_code == 200 and "Drivers" in r.text and "2 tasks" in r.text
