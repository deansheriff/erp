"""Procurement through the browser's eyes (Phase 11c): an employee raises a requisition,
the administrator steps in for the missing line manager, starts an RFQ, records a quote,
awards it and makes the draft purchase order; supplier performance and settings render
and save."""

import html
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import clock
from app.core.db import session_scope
from app.modules.finance.models import Account, Supplier, WhtType
from app.modules.procurement.models import Requisition, RequisitionLine, Rfq
from tests.conftest import csrf_token
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)
from tests.web.test_hr_screens import _add_and_invite

pytestmark = pytest.mark.db


def _post(client: TestClient, url: str, data: dict[str, Any] | None = None) -> Any:
    return client.post(url, data={**(data or {}), "csrf_token": csrf_token(client)})


def _one(model: type, **where: object) -> str:
    with session_scope() as db:
        query = select(model.id)  # type: ignore[attr-defined]
        for key, value in where.items():
            query = query.where(getattr(model, key) == value)
        return str(db.scalars(query).one())


def test_every_procurement_page_renders(admin: TestClient) -> None:  # noqa: F811
    for page in (
        "/procurement/requisitions",
        "/procurement/requisitions?tab=approve",
        "/procurement/requisitions?tab=sourcing",
        "/procurement/requisitions?tab=all",
        "/procurement/requisitions/new",
        "/procurement/rfqs",
        "/procurement/rfqs?tab=all",
        "/procurement/suppliers",
        "/procurement/suppliers?months=3",
        "/procurement/settings",
    ):
        assert admin.get(page).status_code == 200, page
    assert admin.get("/procurement", follow_redirects=False).status_code == 303
    nav = admin.get("/procurement/rfqs").text
    for href in ("/procurement/requisitions", "/procurement/suppliers", "/procurement/settings"):
        assert f'href="{href}"' in nav
    r = _post(
        admin,
        "/procurement/requisitions",
        {"purpose": "Pens", "description": ["Pens"], "quantity": ["1"], "estimated_price": ["500"]},
    )
    assert r.status_code == 422 and "staff list" in html.unescape(r.text)

    r = _post(
        admin,
        "/procurement/settings",
        {
            "quote_threshold": "1,500,000.00",
            "above_finance": "500,000",
            "above_director": "1",
            "active_finance": "1",
            "active_director": "1",
        },
    )
    assert r.status_code == 422
    assert "must be over the Finance Manager" in html.unescape(r.text)
    r = _post(
        admin,
        "/procurement/settings",
        {
            "quote_threshold": "1,500,000.00",
            "above_finance": "500,000",
            "above_director": "5,000,000",
            "active_finance": "1",
            "active_director": "1",
        },
    )
    assert r.status_code == 200 and "Settings saved." in r.text
    assert "1,500,000.00" in r.text


def test_a_requisition_goes_out_for_quotes_and_becomes_an_order(
    admin: TestClient,  # noqa: F811
) -> None:
    _, amaka = _add_and_invite(admin)
    lines = {
        "item_id": ["", ""],
        "warehouse_id": ["", ""],
        "description": ["Printer servicing", ""],
        "account_id": ["", ""],
        "quantity": ["2", ""],
        "estimated_price": ["45,000", ""],
    }
    r = _post(
        amaka,
        "/procurement/requisitions",
        {"purpose": "Service the office printers", "then": "submit", **lines},
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "PR-00001" in page and "Submitted for approval." in page
    assert "Waiting for a Director (there's no line manager)" in page
    requisition = _one(Requisition, number="PR-00001")

    r = _post(admin, f"/procurement/requisitions/{requisition}/reject")
    assert r.status_code == 422 and "Say why" in html.unescape(r.text)
    r = _post(admin, f"/procurement/requisitions/{requisition}/approve", {"note": "Go ahead"})
    page = html.unescape(r.text)
    assert "Approved. Procurement can now ask for quotes." in page and "Start RFQ" in page

    _post(
        admin,
        "/finance/suppliers",
        {
            "name": "Ikeja Office Care",
            "kind": "company",
            "email": "quotes@ikejacare.example",
            "wht_type_id": _one(WhtType, key="supply_of_goods"),
        },
    )
    supplier = _one(Supplier, name="Ikeja Office Care")
    r = _post(admin, f"/procurement/requisitions/{requisition}/rfq", {"supplier_id": [supplier]})
    page = html.unescape(r.text)
    assert r.status_code == 200 and "RFQ-00001" in page and "Record quote" in page
    rfq = _one(Rfq, number="RFQ-00001")
    r = _post(admin, f"/procurement/rfqs/{rfq}/send")
    assert "Emailed to 1 supplier." in html.unescape(r.text)
    pdf = admin.get(f"/procurement/rfqs/{rfq}/pdf?supplier={supplier}")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    line = _one(RequisitionLine, description="Printer servicing")
    quote_url = f"/procurement/rfqs/{rfq}/quotes/{supplier}"
    assert admin.get(quote_url).status_code == 200
    r = _post(
        admin,
        quote_url,
        {"reference": "IOC-77", "received_on": clock.today().isoformat(), "delivery_days": "3"},
    )
    assert r.status_code == 422 and "Enter the supplier's unit price." in html.unescape(r.text)
    r = _post(
        admin,
        quote_url,
        {
            "reference": "IOC-77",
            "received_on": clock.today().isoformat(),
            "delivery_days": "3",
            f"price_{line}": "42,500",
            f"vat_{line}": "exempt",
        },
    )
    page = html.unescape(r.text)
    assert "Quote recorded." in page and "Comparison" in page and "₦85,000.00" in page

    quote = page.split('name="quote_id" value="')[1].split('"')[0]
    r = _post(admin, f"/procurement/rfqs/{rfq}/award", {"quote_id": quote})
    assert "Awarded. Make the purchase order" in html.unescape(r.text)
    r = _post(admin, f"/procurement/rfqs/{rfq}/order")
    assert r.status_code == 422 and "Choose the account to charge." in html.unescape(r.text)
    account = _one(Account, code="6300")
    r = _post(admin, f"/procurement/rfqs/{rfq}/order", {f"account_{line}": account})
    page = html.unescape(r.text)
    assert "Draft purchase order made." in page and 'href="/finance/purchase-orders/' in page

    performance = admin.get(f"/procurement/suppliers/{supplier}")
    assert performance.status_code == 200 and "RFQ-00001" in performance.text
    assert "Won" in performance.text
    mine = html.unescape(amaka.get(f"/procurement/requisitions/{requisition}").text)
    assert "Ordered" in mine and uuid.UUID(requisition)
