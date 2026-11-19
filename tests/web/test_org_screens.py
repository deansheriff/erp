"""Phase 4 screens end to end: building the structure through the forms, the org chart,
people, organisation details, and permission checks on every page."""

import html
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.hr.models import Employee
from app.org.models import Department, Organization, Position
from tests.conftest import Installed, csrf_token, sign_in
from tests.identity.test_accounts import _accept, _invite

pytestmark = pytest.mark.db

PAGES = [
    "/hr/org-chart",
    "/hr/departments",
    "/hr/people",
    "/hr/structure",
    "/hr/structure?tab=grades",
    "/admin/organisation",
]


@pytest.fixture()
def admin(installed: Installed, client: TestClient) -> TestClient:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    return client


def _post(client: TestClient, url: str, **data: str) -> object:
    return client.post(url, data={**data, "csrf_token": csrf_token(client)})


def _id(model: type, **where: str) -> uuid.UUID:
    with session_scope() as db:
        stmt = select(model.id)  # type: ignore[attr-defined]
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return db.scalars(stmt).one()


def test_every_page_renders_for_the_super_admin(admin: TestClient) -> None:
    for page in PAGES:
        response = admin.get(page)
        assert response.status_code == 200, page
    nav = admin.get("/hr/org-chart").text
    for href in ("/hr/org-chart", "/hr/departments", "/hr/people", "/hr/structure"):
        assert f'href="{href}"' in nav


def test_people_without_permission_are_refused(installed: Installed, client: TestClient) -> None:
    token = _invite(client, installed)
    employee = _accept(token, client.app)
    for page in PAGES:
        assert employee.get(page).status_code == 403, page
    assert "/hr/org-chart" not in employee.get("/").text


def test_building_a_structure_through_the_forms(admin: TestClient) -> None:
    r = _post(admin, "/hr/structure/grades", code="gl-12", name="Manager", rank="12")
    assert r.status_code == 200 and "GL-12" in r.text  # type: ignore[attr-defined]
    _post(admin, "/hr/departments", code="FIN", name="Finance")
    fin = _id(Department, code="FIN")
    _post(admin, "/hr/departments", code="TRE", name="Treasury", parent_id=str(fin))
    tree = admin.get("/hr/departments").text
    assert "Treasury" in tree and "Finance" in tree

    r = _post(admin, "/hr/positions", title="Head of Finance", department_id=str(fin))
    assert r.status_code == 200, r.text[:300]  # type: ignore[attr-defined]
    head = _id(Position, title="Head of Finance")
    _post(admin, "/hr/positions", title="Accountant", department_id=str(fin), parent_id=str(head))

    _post(
        admin,
        "/hr/people",
        first_name="Bola",
        last_name="Ade",
        work_email="bola.ade@adeyemiholdings.ng",
    )
    bola = _id(Employee, first_name="Bola")
    r = _post(
        admin,
        f"/hr/positions/{head}/assign",
        employee_id=str(bola),
        kind="primary",
        starts="2026-01-05",
        reason="Founding head of finance",
    )
    assert r.status_code == 200, r.text[:300]  # type: ignore[attr-defined]
    page = admin.get(f"/hr/positions/{head}").text
    assert "Bola Ade" in page and "Primary" in page

    chart = admin.get("/hr/org-chart").text
    assert "Head of Finance" in chart and "Bola Ade" in chart
    assert "Vacant" in chart  # the Accountant position
    accountant = _id(Position, title="Accountant")
    detail = admin.get(f"/hr/positions/{accountant}").text
    assert "Head of Finance" in detail and "Bola Ade" in detail  # reporting line


def test_cycles_are_refused_in_the_form(admin: TestClient) -> None:
    _post(admin, "/hr/departments", code="FIN", name="Finance")
    fin = _id(Department, code="FIN")
    _post(admin, "/hr/departments", code="TRE", name="Treasury", parent_id=str(fin))
    tre = _id(Department, code="TRE")
    r = _post(
        admin,
        "/hr/departments",
        department_id=str(fin),
        code="FIN",
        name="Finance",
        parent_id=str(tre),
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "own sub-departments" in html.unescape(r.text)  # type: ignore[attr-defined]


def test_assignment_errors_show_in_the_drawer(admin: TestClient) -> None:
    _post(admin, "/hr/departments", code="FIN", name="Finance")
    _post(
        admin,
        "/hr/positions",
        title="Head of Finance",
        department_id=str(_id(Department, code="FIN")),
    )
    head = _id(Position, title="Head of Finance")
    r = _post(
        admin, f"/hr/positions/{head}/assign", employee_id="", kind="primary", starts="", reason=""
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "Choose a person" in r.text  # type: ignore[attr-defined]


def test_filters_on_the_org_chart(admin: TestClient) -> None:
    _post(admin, "/hr/departments", code="FIN", name="Finance")
    _post(
        admin,
        "/hr/positions",
        title="Treasury Lead",
        department_id=str(_id(Department, code="FIN")),
    )
    assert "Treasury Lead" in admin.get("/hr/org-chart?vacant=1").text
    assert "No matches" in admin.get("/hr/org-chart?q=nobody-here").text


def test_people_can_be_edited_and_unknown_people_are_404(admin: TestClient) -> None:
    _post(admin, "/hr/people", first_name="Efe", last_name="Nwosu")
    efe = _id(Employee, first_name="Efe")
    r = _post(admin, f"/hr/people/{efe}", first_name="Efe", last_name="Okon")
    assert r.status_code == 200  # type: ignore[attr-defined]
    assert "Efe Okon" in admin.get("/hr/people").text
    assert admin.get(f"/hr/people?edit={uuid.uuid4()}").status_code == 404


def test_organisation_details_and_brand_colours(admin: TestClient) -> None:
    r = _post(
        admin,
        "/admin/organisation",
        legal_name="Adeyemi Holdings Limited",
        rc_number="RC 1234567",
        fiscal_year_start_month="4",
        brand_primary="#5a4bdb",
    )
    assert r.status_code == 200, r.text[:300]  # type: ignore[attr-defined]
    with session_scope() as db:
        row = db.scalars(select(Organization)).one()
        assert (row.rc_number, row.fiscal_year_start_month, row.brand_primary) == (
            "RC 1234567",
            4,
            "#5A4BDB",
        )
        assert row.timezone == "Africa/Lagos"
    r = _post(
        admin, "/admin/organisation", legal_name="Adeyemi Holdings Limited", brand_primary="purple"
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert re.search(r"Use a colour like", html.unescape(r.text))  # type: ignore[attr-defined]
