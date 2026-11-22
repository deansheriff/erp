"""Receivables and tax through the browser's eyes (Phase 10b): add a customer, raise and
issue an invoice, record a receipt with WHT, see aging and statements, and the tax pages."""

import html

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.finance.models import Account, Customer, Receipt, SalesDocument
from tests.conftest import csrf_token
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)
from tests.web.test_hr_screens import _post

pytestmark = pytest.mark.db


def _account(code: str) -> str:
    with session_scope() as db:
        return str(db.scalars(select(Account.id).where(Account.code == code)).one())


def _one(model: type, **where: object) -> str:
    with session_scope() as db:
        query = select(model.id)  # type: ignore[attr-defined]
        for key, value in where.items():
            query = query.where(getattr(model, key) == value)
        return str(db.scalars(query).one())


def test_customer_invoice_receipt_and_aging(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", year="2026")
    for page in ("/finance/customers", "/finance/sales", "/finance/aging", "/finance/tax"):
        assert admin.get(page).status_code == 200, page
    r = _post(admin, "/finance/customers", name="", email="nope", reminders="1")
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "Enter the customer's name." in html.unescape(r.text)  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/finance/customers",
        name="Adebayo Foods Ltd",
        email="accounts@adebayo.example",
        payment_terms_days="30",
        reminders="1",
    )
    assert r.status_code == 200 and "Adebayo Foods Ltd" in r.text  # type: ignore[attr-defined]
    customer = _one(Customer, name="Adebayo Foods Ltd")

    r = admin.post(
        "/finance/sales",
        data={
            "kind": "invoice",
            "customer_id": customer,
            "doc_date": "2026-10-05",
            "description": ["Rice, 50kg bags"],
            "account_id": [_account("4100")],
            "cost_centre_id": [""],
            "quantity": ["4"],
            "unit_price": ["25,000"],
            "vat_code": ["standard"],
            "csrf_token": csrf_token(admin),
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "Draft saved." in page and "Issue invoice" in page
    invoice = _one(SalesDocument, kind="invoice")
    r = _post(admin, f"/finance/sales/{invoice}/issue")
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert "INV-00001" in page and "₦107,500.00" in page and "Issued and posted." in page
    assert admin.get(f"/finance/sales/{invoice}/pdf").content.startswith(b"%PDF")
    r = _post(admin, f"/finance/sales/{invoice}/email")
    assert "Email queued to send." in html.unescape(r.text)  # type: ignore[attr-defined]

    # A credit note drafted by the admin needs someone else to issue it.
    r = _post(admin, f"/finance/sales/{invoice}/credit-note")
    assert "Credit note drafted." in html.unescape(r.text)  # type: ignore[attr-defined]
    note = _one(SalesDocument, kind="credit_note")
    r = _post(admin, f"/finance/sales/{note}/issue")
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "someone else issues it" in html.unescape(r.text)  # type: ignore[attr-defined]

    r = admin.get(f"/finance/receipts/new?customer={customer}")
    assert "INV-00001" in r.text
    with session_scope() as db:
        wht_type = str(
            db.scalars(select(Account.id).where(Account.code == "1010")).one()
        )  # any id that isn't a WHT type
    r = admin.post(
        "/finance/receipts",
        data={
            "customer_id": customer,
            "receipt_date": "2026-10-20",
            "bank_account_id": _account("1010"),
            "cash": "100,000",
            "wht": "5,000",
            "wht_type_id": wht_type,
            "invoice_id": [invoice],
            "allocate": ["105,000"],
            "csrf_token": csrf_token(admin),
        },
    )
    assert r.status_code == 422
    assert "Choose the type of withholding tax deducted." in html.unescape(r.text)
    r = admin.post(
        "/finance/receipts",
        data={
            "customer_id": customer,
            "receipt_date": "2026-10-20",
            "bank_account_id": _account("1010"),
            "cash": "100,000",
            "wht": "",
            "invoice_id": [invoice],
            "allocate": ["100,000"],
            "csrf_token": csrf_token(admin),
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "RCT-00001" in page and "Receipt recorded and posted." in page
    receipt = _one(Receipt, number="RCT-00001")
    assert "Overdue" in admin.get(f"/finance/sales/{invoice}").text  # due 4 November

    page = html.unescape(admin.get(f"/finance/customers/{customer}").text)
    assert "₦7,500.00" in page and "Statement" in page
    assert admin.get(
        f"/finance/customers/{customer}/statement.pdf?start=2026-10-01&end=2026-10-31"
    ).content.startswith(b"%PDF")
    r = _post(admin, f"/finance/customers/{customer}/remind")
    assert "Reminder queued to send." in html.unescape(r.text)  # type: ignore[attr-defined]
    r = admin.get("/finance/aging?as_at=2026-12-31")
    assert "Adebayo Foods Ltd" in r.text and "₦7,500.00" in html.unescape(r.text)
    r = admin.get("/finance/aging.csv?as_at=2026-12-31")
    assert r.text.startswith("Code,Customer,Current") and "7500.00" in r.text

    r = _post(admin, f"/finance/receipts/{receipt}/reverse", reverse_on="2026-10-25", reason="")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(
        admin, f"/finance/receipts/{receipt}/reverse", reverse_on="2026-10-25", reason="Bounced"
    )
    assert "Receipt reversed." in html.unescape(r.text)  # type: ignore[attr-defined]
    for tab in ("invoice", "quote", "order", "credit_note", "receipt"):
        assert admin.get(f"/finance/sales?tab={tab}").status_code == 200, tab


def test_tax_settings_and_returns(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", year="2026")
    page = html.unescape(admin.get("/finance/tax").text)
    assert "7.5%" in page and "Rate not set" in page and "Supply of goods" in page
    r = _post(admin, "/finance/tax/vat", effective_from="2020-02-01", rate="8")
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "A rate already starts on that date." in html.unescape(r.text)  # type: ignore[attr-defined]
    r = _post(admin, "/finance/tax/stamp", stamp_name="Agreement", basis="flat", value="500")
    assert r.status_code == 200 and "Agreement" in r.text  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/finance/tax/settings",
        receivables_code="1100",
        vat_output_code="2200",
        vat_input_code="1310",
        wht_receivable_code="1320",
        wht_payable_code="2210",
        stamp_duty_code="2220",
        default_terms_days="400",
        remind_before_days="3",
        remind_every_days="7",
        return_due_day="21",
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "Enter a whole number from 0 to 365." in html.unescape(r.text)  # type: ignore[attr-defined]
    for tax in ("vat", "wht", "stamp"):
        r = admin.get(f"/finance/tax/returns?tax={tax}")
        assert r.status_code == 200 and "January 2026" in r.text, tax
    r = _post(admin, "/finance/tax/returns/vat/2026-01/file", on="2026-02-10", reference="")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(admin, "/finance/tax/returns/vat/2026-01/file", on="2026-02-10", reference="TP-1")
    assert "Marked as filed." in html.unescape(r.text)  # type: ignore[attr-defined]
    r = _post(admin, "/finance/tax/returns/vat/2026-01/paid", on="2026-02-12", reference="RRR")
    assert "Marked as paid." in html.unescape(r.text)  # type: ignore[attr-defined]
