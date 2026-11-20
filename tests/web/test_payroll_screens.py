"""Payroll through the browser's eyes: pay is set for an employee, a run is created and
calculated, the preparer can't review their own run, rent relief is declared and
verified, and the employee sees the payslip only once the run is paid. The full
step-by-step maker-checker path is covered in tests/payroll."""

import html
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import clock
from app.core.db import session_scope
from app.modules.payroll.models import (
    PayComponent,
    PayGroup,
    PayrollRun,
    RentDeclaration,
    SalaryStructure,
    StaffLoan,
)
from tests.conftest import Installed, csrf_token, sign_in
from tests.web.test_hr_screens import _add_and_invite, _post

pytestmark = pytest.mark.db

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n" + b"\x00" * 100
PAGES = (
    "/payroll",
    "/payroll/people",
    "/payroll/structures",
    "/payroll/reliefs",
    "/payroll/reliefs?tab=verified",
    "/payroll/settings",
    "/payroll/settings?new=1",
    "/payslips",
)


@pytest.fixture()
def admin(installed: Installed, client: TestClient) -> TestClient:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    return client


def _ids() -> tuple[str, str, dict[str, str]]:
    with session_scope() as db:
        group = db.scalars(select(PayGroup.id)).one()
        structure = db.scalars(select(SalaryStructure.id)).one()
        codes = {c.code: str(c.id) for c in db.scalars(select(PayComponent))}
    return str(group), str(structure), codes


def test_every_payroll_page_renders_for_the_super_admin(admin: TestClient) -> None:
    for page in PAGES:
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/payroll").text
    for href in ("/payroll/people", "/payroll/structures", "/payroll/reliefs", "/payroll/settings"):
        assert f'href="{href}"' in nav
    assert "No employee record" in admin.get("/payslips").text
    settings = admin.get("/payroll/settings").text
    assert "PAYE bands" in settings and 'value="800,000.00"' in settings


def test_pay_run_and_payslip(admin: TestClient) -> None:
    person, amaka = _add_and_invite(admin)
    assert amaka.get("/payroll").status_code == 403
    assert "No payslips yet" in amaka.get("/payslips").text
    group, structure, codes = _ids()

    form = {
        "pay_group_id": group,
        "structure_id": structure,
        "start_date": "2026-01-01",
        "reason": "Starting pay",
        "component_id": [codes["basic"], codes["housing"], codes["transport"]],
        "amount": ["abc", "100,000", "50,000"],
        "csrf_token": csrf_token(admin),
    }
    r = admin.post(f"/payroll/people/{person}/pay", data=form)
    assert r.status_code == 422
    form["amount"] = ["200,000", "100,000", "50,000"]
    r = admin.post(f"/payroll/people/{person}/pay", data=form)
    assert r.status_code == 200 and "350,000.00" in r.text

    r = _post(admin, "/payroll/runs", pay_group_id=group, period="2026-01")
    assert r.status_code == 200, r.text[:500]  # type: ignore[attr-defined]
    run_url = str(r.url).split("?", maxsplit=1)[0]  # type: ignore[attr-defined]
    run_id = run_url.rsplit("/", 1)[1]
    r = _post(admin, f"/payroll/runs/{run_id}/calculate")
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert r.status_code == 200 and "Amaka Okafor" in page and "350,000.00" in page  # type: ignore[attr-defined]
    r = _post(admin, f"/payroll/runs/{run_id}/review")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(admin, f"/payroll/runs/{run_id}/send_back", note="")
    assert r.status_code == 422  # type: ignore[attr-defined]

    # The other steps need different people (tests/payroll); here the run is moved to
    # paid directly so the employee's view can be checked.
    assert "No payslips yet" in amaka.get("/payslips").text
    with session_scope() as db:
        run = db.get(PayrollRun, run_id)
        assert run is not None
        run.status = "paid"
        run.paid_at = clock.now()
    mine = html.unescape(amaka.get("/payslips").text)
    assert "January 2026" in mine and "Basic" in mine and "PAYE" in mine


def test_rent_relief_declared_and_verified(admin: TestClient) -> None:
    _, amaka = _add_and_invite(admin)
    year = str(clock.today().year)
    r = amaka.post(
        "/payslips/rent",
        data={"tax_year": year, "annual_rent": ""},
        files={"file": ("lease.pdf", PDF, "application/pdf")},
        headers={"X-CSRF-Token": csrf_token(amaka)},
    )
    assert r.status_code == 422
    r = amaka.post(
        "/payslips/rent",
        data={"tax_year": year, "annual_rent": "1,800,000"},
        files={"file": ("lease.pdf", PDF, "application/pdf")},
        headers={"X-CSRF-Token": csrf_token(amaka)},
    )
    assert r.status_code == 200 and "Rent declared" in r.text and "Pending" in r.text

    queue = admin.get("/payroll/reliefs").text
    assert "Amaka Okafor" in queue and "1,800,000.00" in queue
    with session_scope() as db:
        declaration = db.scalars(select(RentDeclaration.id)).one()
    r = _post(admin, f"/payroll/reliefs/{declaration}/reject", note="")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(admin, f"/payroll/reliefs/{declaration}/verify", note="")
    assert r.status_code == 200 and "Verified." in r.text  # type: ignore[attr-defined]
    assert "Verified" in amaka.get("/payslips").text


def test_components_structures_groups_and_settings(admin: TestClient) -> None:
    r = _post(admin, "/payroll/structures/components", code="Bad Code", name="")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/payroll/structures/components",
        code="utility",
        name="Utility allowance",
        kind="earning",
        calculation="fixed",
        taxable="1",
    )
    assert r.status_code == 200 and "Utility allowance" in r.text  # type: ignore[attr-defined]
    r = _post(admin, "/payroll/structures/groups", group_name="Contract staff")
    assert r.status_code == 200 and "Contract staff" in r.text  # type: ignore[attr-defined]

    r = admin.post(
        "/payroll/settings",
        data={
            "effective_from": "",
            "label": "2027 rates",
            "band_width": ["800,000", ""],
            "band_rate": ["0", "15"],
            "csrf_token": csrf_token(admin),
        },
    )
    assert r.status_code == 422
    assert re.search(r'name="label"[^>]*value="2027 rates"', r.text)


def test_settings_save_unchanged_from_what_the_form_shows(admin: TestClient) -> None:
    page = admin.get("/payroll/settings").text
    form = page[page.index('action="/payroll/settings"') :]
    form = form[: form.index("</form>")]
    data: dict[str, list[str]] = {}
    for tag in re.findall(r"<input[^>]*>", form):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            text = html.unescape(value.group(1)) if value else ""
            data.setdefault(name.group(1), []).append(text)
    assert data["nhis_rate"] == ["0"] and data["pension_employee_rate"] == ["8"]
    assert data["band_width"][0] == "800,000.00" and data["band_rate"][5] == "25"
    r = admin.post("/payroll/settings", data=data)
    assert r.status_code == 200 and "Saved." in r.text


def test_phase_9b_pages_render(admin: TestClient) -> None:
    for page in (
        "/payroll/remittances",
        "/payroll/loans",
        "/payroll/loans?tab=repaying",
        "/payroll/loans?tab=cleared",
        "/payroll/loans?tab=rejected",
        "/payroll/certificates",
    ):
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/payroll").text
    for href in ("/payroll/remittances", "/payroll/loans", "/payroll/certificates"):
        assert f'href="{href}"' in nav
    settings = admin.get("/payroll/settings").text
    assert "Ledger accounts" in settings and "Guaranty Trust Bank" in settings


def test_loans_remittances_and_my_payslips(admin: TestClient) -> None:
    person, amaka = _add_and_invite(admin)
    nav = amaka.get("/payslips").text
    assert 'href="/payroll/loans"' not in nav and "Tax certificates" in nav
    group, structure, codes = _ids()
    admin.post(
        f"/payroll/people/{person}/pay",
        data={
            "pay_group_id": group,
            "structure_id": structure,
            "start_date": "2026-01-01",
            "reason": "Starting pay",
            "component_id": [codes["basic"], codes["housing"], codes["transport"]],
            "amount": ["200,000", "100,000", "50,000"],
            "csrf_token": csrf_token(admin),
        },
    )

    r = _post(admin, "/payroll/loans", employee_id=str(person), kind="loan", amount="")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/payroll/loans",
        employee_id=str(person),
        kind="loan",
        amount="120,000",
        instalment="40,000",
        first_month=clock.today().strftime("%Y-%m"),
    )
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert r.status_code == 200 and "LN-00001" in page and "Amaka Okafor" in page  # type: ignore[attr-defined]
    with session_scope() as db:
        loan_id = db.scalars(select(StaffLoan.id)).one()
    r = _post(admin, f"/payroll/loans/{loan_id}/approve")
    assert r.status_code == 422  # type: ignore[attr-defined]  (Fatima recorded it)
    assert "Someone else approves this loan" in html.unescape(r.text)  # type: ignore[attr-defined]
    assert "LN-00001" in amaka.get("/payslips").text

    r = _post(admin, "/payroll/runs", pay_group_id=group, period="2026-11")
    run_id = str(r.url).split("?", maxsplit=1)[0].rsplit("/", 1)[1]  # type: ignore[attr-defined]
    _post(admin, f"/payroll/runs/{run_id}/calculate")
    assert "No approved runs yet" in admin.get("/payroll/remittances").text
    with session_scope() as db:
        run = db.get(PayrollRun, run_id)
        assert run is not None
        run.status = "approved"
    page = html.unescape(admin.get(f"/payroll/remittances?run={run_id}").text)
    assert "Bank payment file" in page and "1 of 1 people" in page
    r = admin.get(f"/payroll/remittances/{run_id}/bank.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "Amaka" in r.text and ",058," in r.text  # GTBank on her HR record
    assert r.headers["cache-control"] == "no-store"
    assert amaka.get(f"/payroll/remittances/{run_id}/bank.csv").status_code == 403
    r = admin.get(f"/payroll/certificates/2026/{person}/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_bank_codes_and_ledger_accounts(admin: TestClient) -> None:
    r = _post(admin, "/payroll/settings/banks", bank_name="Bank of Nowhere", bank_code="x")
    assert r.status_code == 422 and "CBN code" in r.text  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/payroll/settings/banks",
        bank_name="Bank of Nowhere",
        bank_code="999",
        other_names="BoN",
    )
    assert r.status_code == 200 and "Bank of Nowhere" in r.text  # type: ignore[attr-defined]
    r = _post(admin, "/payroll/settings/accounts", salaries="6000")
    assert r.status_code == 422 and "Enter an account code" in r.text  # type: ignore[attr-defined]
