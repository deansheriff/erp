"""Payables through the browser's eyes (Phase 10c): add a supplier, draft a purchase order
and a bill the drafter can't approve, and every payables page."""

import html
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.finance.models import Account, Bill, PurchaseOrder, Supplier, WhtType
from tests.conftest import csrf_token
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)
from tests.web.test_hr_screens import _post

pytestmark = pytest.mark.db


def _id(model: type, **where: object) -> str:
    with session_scope() as db:
        query = select(model.id)  # type: ignore[attr-defined]
        for key, value in where.items():
            query = query.where(getattr(model, key) == value)
        return str(db.scalars(query).one())


def _lines(account: str, price: str) -> dict[str, list[str]]:
    return {
        "description": ["A4 paper, boxes"],
        "account_id": [account],
        "cost_centre_id": [""],
        "quantity": ["10"],
        "unit_price": [price],
        "vat_code": ["standard"],
    }


def test_suppliers_orders_bills_and_payments(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", year="2026")
    for page in (
        "/finance/suppliers",
        "/finance/suppliers?edit=new",
        "/finance/purchases",
        "/finance/purchases?tab=order",
        "/finance/purchases?tab=debit_note",
        "/finance/purchases?tab=payment",
        "/finance/purchase-orders/new",
        "/finance/bills/new",
        "/finance/payments/new",
        "/finance/payables-aging",
    ):
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/finance/suppliers").text
    for href in ("/finance/purchases", "/finance/payables-aging"):
        assert f'href="{href}"' in nav

    r = _post(
        admin,
        "/finance/suppliers",
        name="Lagos Office Supplies Ltd",
        kind="company",
        bank_code="58",
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert "Enter the bank's CBN code, 3 to 6 digits." in page
    assert "Enter the 10-digit account number." in page
    r = _post(
        admin,
        "/finance/suppliers",
        name="Lagos Office Supplies Ltd",
        kind="company",
        wht_type_id=_id(WhtType, key="supply_of_goods"),
        bank_name="GTBank",
        bank_code="058",
        account_number="0123454821",
        account_name="Lagos Office Supplies Ltd",
    )
    assert r.status_code == 200 and "GTBank" in r.text  # type: ignore[attr-defined]
    supplier = _id(Supplier, name="Lagos Office Supplies Ltd")
    assert "GTBank ••••4821" in admin.get("/finance/suppliers").text

    account = _id(Account, code="6300")
    r = admin.post(
        "/finance/purchase-orders",
        data={
            "supplier_id": supplier,
            "order_date": "2026-09-01",
            "csrf_token": csrf_token(admin),
            **_lines(account, "10,000"),
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "Someone other than you approves it." in page
    assert "You drafted this purchase order, so someone else approves it." in page
    order = _id(PurchaseOrder, supplier_id=uuid.UUID(supplier))
    r = _post(admin, f"/finance/purchase-orders/{order}/approve")
    assert r.status_code == 422  # type: ignore[attr-defined]

    r = admin.post(
        "/finance/bills",
        data={
            "kind": "bill",
            "supplier_id": supplier,
            "supplier_reference": "LOS/881",
            "bill_date": "2026-09-15",
            "order_line_id": [""],
            "csrf_token": csrf_token(admin),
            **_lines(account, "1,000"),
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "Draft saved." in page and "LOS/881" in page
    bill = _id(Bill, supplier_reference="LOS/881")
    r = _post(admin, f"/finance/bills/{bill}/approve")
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "someone else approves it" in html.unescape(r.text)  # type: ignore[attr-defined]

    r = admin.get(f"/finance/payments/new?supplier={supplier}")
    assert "no posted bills waiting to be paid" in html.unescape(r.text)
    assert admin.get(f"/finance/suppliers/{supplier}").status_code == 200
    r = admin.get("/finance/payables-aging.csv")
    assert r.text.startswith("Code,Supplier,Current")
