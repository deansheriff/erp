"""Stock in purchasing and sales through the browser's eyes (Phase 11b, ADR 0029): a
purchase order with a lot-tracked stock line, goods received with a lot and expiry, the
bill made from it, and an invoice that can't take more than is on hand."""

import html
import uuid

import pytest
from fastapi.testclient import TestClient

from app.modules.finance.models import (
    Account,
    Customer,
    GoodsReceipt,
    PurchaseOrder,
    SalesDocument,
    Supplier,
    WhtType,
)
from app.modules.inventory.models import Item, ItemCategory, Unit, Warehouse
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)
from tests.web.test_inventory_screens import _as_if_someone_else, _one, _post

pytestmark = pytest.mark.db


def _text(r: object) -> str:
    return html.unescape(r.text)  # type: ignore[attr-defined]


def _status(r: object) -> int:
    return int(r.status_code)  # type: ignore[attr-defined]


def test_a_stock_order_is_received_with_its_lot_and_billed(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", {"year": "2026"})
    r = _post(
        admin,
        "/inventory/items/new",
        {
            "name": "Flour 25kg",
            "category_id": _one(ItemCategory, name="General"),
            "unit_id": _one(Unit, code="EA"),
            "valuation": "fifo",
            "tracking": "lot",
        },
    )
    assert _status(r) == 200
    item, main = _one(Item, code="ITM-00001"), _one(Warehouse, code="MAIN")
    _post(
        admin,
        "/finance/suppliers",
        {
            "name": "Kano Grains Ltd",
            "kind": "company",
            "wht_type_id": _one(WhtType, key="supply_of_goods"),
        },
    )
    supplier = _one(Supplier, name="Kano Grains Ltd")
    assert "Stock item" in admin.get("/finance/purchase-orders/new").text

    lines = {
        "item_id": [item],
        "warehouse_id": [main],
        "description": [""],
        "account_id": [""],
        "cost_centre_id": [""],
        "quantity": ["5"],
        "unit_price": ["1,000"],
        "vat_code": ["exempt"],
    }
    r = _post(
        admin,
        "/finance/purchase-orders",
        {"supplier_id": supplier, "order_date": "2026-10-01", **lines},
    )
    page = _text(r)
    assert _status(r) == 200 and "ITM-00001 Flour 25kg" in page
    order = _one(PurchaseOrder, supplier_id=uuid.UUID(supplier))
    _as_if_someone_else(PurchaseOrder, order)
    r = _post(admin, f"/finance/purchase-orders/{order}/approve")
    page = _text(r)
    assert "Record goods received" in page and "Lot number" in page and "Into Main" in page

    line = page.split('name="line_id" value="')[1].split('"')[0]
    grn = {
        "received_on": "2026-10-03",
        "line_id": [line],
        "received": ["5"],
        "bin_id": [""],
        "lot_number": [""],
        "expiry": [""],
        "serials": [""],
    }
    r = _post(admin, f"/finance/purchase-orders/{order}/receive", grn)
    assert _status(r) == 422 and "lot number" in _text(r).lower()
    grn |= {"lot_number": ["L-0926"], "expiry": ["2027-03-31"]}
    r = _post(admin, f"/finance/purchase-orders/{order}/receive", grn)
    page = _text(r)
    assert _status(r) == 200 and "Goods received recorded." in page
    assert _one(GoodsReceipt, order_id=uuid.UUID(order))
    assert 'href="/finance/journals/' in page  # the GRN's stock journal
    ledger = _text(admin.get(f"/inventory/items/{item}"))
    assert "GRN-00001" in ledger and "Goods received" in ledger

    r = _post(admin, f"/finance/purchase-orders/{order}/bill")
    page = _text(r)
    assert _status(r) == 200 and "ITM-00001 Flour 25kg" in page
    assert "clear goods received not invoiced at the order price" in page


def test_an_invoice_names_its_stock_and_is_refused_without_enough(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", {"year": "2026"})
    _post(
        admin,
        "/inventory/items/new",
        {
            "name": "Beans 50kg",
            "category_id": _one(ItemCategory, name="General"),
            "unit_id": _one(Unit, code="EA"),
        },
    )
    item, main = _one(Item, code="ITM-00001"), _one(Warehouse, code="MAIN")
    _post(
        admin,
        "/finance/customers",
        {
            "name": "Adebayo Foods Ltd",
            "email": "accounts@adebayo.example",
            "payment_terms_days": "30",
            "is_active": "on",
        },
    )
    customer = _one(Customer, name="Adebayo Foods Ltd")
    page = admin.get("/finance/sales/new?kind=invoice").text
    assert "Stock item" in page and "Serial numbers, for serial items" in page

    r = _post(
        admin,
        "/finance/sales",
        {
            "kind": "invoice",
            "customer_id": customer,
            "doc_date": "2026-10-06",
            "item_id": [item],
            "warehouse_id": [main],
            "serials": [""],
            "description": [""],
            "account_id": [_one(Account, code="4100")],
            "cost_centre_id": [""],
            "quantity": ["2"],
            "unit_price": ["8,000"],
            "vat_code": ["exempt"],
        },
    )
    assert _status(r) == 200 and "Beans 50kg" in _text(r)
    invoice = _one(SalesDocument, customer_id=uuid.UUID(customer))
    r = _post(admin, f"/finance/sales/{invoice}/issue")
    assert _status(r) == 422 and "Beans 50kg" in _text(r)  # nothing on hand
