"""Expenses through the browser's eyes: an employee writes a claim with a receipt and
sends it, an approver corrects and approves it, it is paid with a journal queued, and
Finance edits settings."""

import html
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import clock
from app.core.db import session_scope
from app.modules.expenses.models import ExpenseCategory, ExpenseDocument, ExpenseLine
from tests.conftest import Installed, csrf_token, sign_in
from tests.web.test_hr_screens import _add_and_invite, _post

pytestmark = pytest.mark.db

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 200


@pytest.fixture()
def admin(installed: Installed, client: TestClient) -> TestClient:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    return client


def _category(name: str) -> str:
    with session_scope() as db:
        return str(db.scalars(select(ExpenseCategory.id).where(ExpenseCategory.name == name)).one())


def test_every_expense_page_renders_for_the_super_admin(admin: TestClient) -> None:
    for page in ("/expenses", "/expenses/approvals", "/expenses/payments", "/expenses/settings"):
        assert admin.get(page).status_code == 200, page
    assert "No HR record yet" in admin.get("/expenses").text
    nav = admin.get("/expenses/settings").text
    for href in ("/expenses/approvals", "/expenses/payments", "/expenses/settings"):
        assert f'href="{href}"' in nav


def test_claim_from_draft_to_paid(admin: TestClient) -> None:
    _, amaka = _add_and_invite(admin)
    page = amaka.get("/expenses")
    assert page.status_code == 200 and "New claim" in page.text
    assert 'href="/expenses/payments"' not in page.text
    r = _post(amaka, "/expenses/new", kind="claim", title="Client visit, Abuja")
    assert r.status_code == 200 and "EXP-00001" in r.text  # type: ignore[attr-defined]
    claim_url = str(r.url)  # type: ignore[attr-defined]
    claim_id = claim_url.rsplit("/", 1)[1].split("?", maxsplit=1)[0]
    r = _post(
        amaka,
        f"/expenses/{claim_id}/lines",
        day=clock.today().isoformat(),
        category_id=_category("Travel"),
        description="Taxi to the airport",
        amount="18,500",
    )
    assert r.status_code == 200 and "Taxi to the airport" in r.text  # type: ignore[attr-defined]
    assert "Receipt needed" in r.text  # type: ignore[attr-defined]
    r = _post(amaka, f"/expenses/{claim_id}/submit")
    assert r.status_code == 422 and "needs a receipt" in html.unescape(r.text)  # type: ignore[attr-defined]
    with session_scope() as db:
        line_id = db.scalars(select(ExpenseLine.id)).one()
    r = amaka.post(
        f"/expenses/lines/{line_id}/receipt",
        files={"file": ("taxi.jpg", JPEG, "image/jpeg")},
        headers={"X-CSRF-Token": csrf_token(amaka)},
    )
    assert r.status_code == 200 and "Receipt uploaded" in r.text
    r = _post(amaka, f"/expenses/{claim_id}/submit")
    assert r.status_code == 200 and "Sent for approval" in r.text  # type: ignore[attr-defined]

    # Amaka has no manager on the org chart, so organisation-wide approvers decide.
    inbox = admin.get("/expenses/approvals").text
    assert "Client visit, Abuja" in inbox and "18,500.00" in inbox
    r = admin.post(
        f"/expenses/{claim_id}/decide/approve",
        data={
            "line_id": str(line_id),
            "line_amount": "15,000",
            "note": "",
            "csrf_token": csrf_token(admin),
        },
    )
    assert r.status_code == 200 and "Approved." in r.text
    with session_scope() as db:
        doc = db.scalars(select(ExpenseDocument)).one()
        assert doc.status == "approved" and str(doc.total) == "15000.00"

    payments = admin.get("/expenses/payments").text
    assert "EXP-00001" in payments
    r = admin.post(
        "/expenses/payments/pay",
        data={
            "tab": "claims",
            "document_id": claim_id,
            "paid_on": clock.today().isoformat(),
            "method": "transfer",
            "reference": "GTB-98765",
            "csrf_token": csrf_token(admin),
        },
    )
    assert r.status_code == 200 and "Marked as paid" in r.text
    paid = admin.get("/expenses/payments?tab=paid").text
    assert "GTB-98765" in paid and "Waiting for Finance" in paid
    mine = amaka.get("/expenses").text
    assert "Paid" in mine and "15,000.00" in mine
    detail = amaka.get(f"/expenses/{claim_id}").text
    assert "changed from" in detail and "18,500.00" in detail


def test_advance_request_and_settings(admin: TestClient) -> None:
    _, amaka = _add_and_invite(admin)
    r = _post(amaka, "/expenses/new", kind="advance", title="Site trip float", amount="")
    assert r.status_code == 422 and "Enter an amount" in r.text  # type: ignore[attr-defined]
    r = _post(amaka, "/expenses/new", kind="advance", title="Site trip float", amount="50000")
    assert r.status_code == 200 and "ADV-00001" in r.text  # type: ignore[attr-defined]

    r = _post(
        admin,
        "/expenses/settings",
        second_level_above="200,000",
        finance_above="100,000",
        weekly_limit="",
        monthly_limit="",
        advances_account="1410",
        bank_account="1010",
    )
    assert r.status_code == 422 and "at least the second-level" in r.text  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/expenses/settings/categories",
        name="Client gifts",
        gl_account="6400",
        claim_limit="25000",
        receipt_above="5000",
    )
    assert r.status_code == 200 and "Client gifts" in r.text  # type: ignore[attr-defined]
    assert re.search(r"limit ₦25,000\.00 per claim", r.text)  # type: ignore[attr-defined]
