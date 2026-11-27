"""Inventory through the browser's eyes (Phase 11a): set up a warehouse and an item, record
an adjustment the recorder can't post, issue stock, count it, and every inventory page."""

import html

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.inventory.models import (
    Item,
    ItemCategory,
    StockCount,
    StockCountLine,
    StockDocument,
    Unit,
    Warehouse,
)
from tests.conftest import csrf_token
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)

pytestmark = pytest.mark.db


def _post(client: TestClient, url: str, data: dict[str, object] | None = None) -> object:
    return client.post(url, data={**(data or {}), "csrf_token": csrf_token(client)})


def _one(model: type, **where: object) -> str:
    with session_scope() as db:
        query = select(model.id)  # type: ignore[attr-defined]
        for key, value in where.items():
            query = query.where(getattr(model, key) == value)
        return str(db.scalars(query).one())


def _as_if_someone_else(model: type, row_id: str) -> None:
    with session_scope() as db:
        row = db.get(model, row_id)
        assert row is not None
        row.created_by = None


def test_pages_and_setup(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", {"year": "2026"})
    for page in (
        "/inventory/items",
        "/inventory/items?view=low",
        "/inventory/items/new",
        "/inventory/setup",
        "/inventory/setup?category=new",
        "/inventory/setup?warehouse=new",
        "/inventory/movements",
        "/inventory/movements?tab=adjustment",
        "/inventory/movements?tab=waiting",
        "/inventory/movements?tab=counts",
        "/inventory/movements/new?kind=issue",
        "/inventory/movements/new?kind=transfer",
        "/inventory/movements/new?kind=adjustment",
        "/inventory/requests",
        "/inventory/requests?tab=all",
        "/inventory/requests/new",
    ):
        assert admin.get(page).status_code == 200, page
    assert admin.get("/inventory").status_code == 200  # redirected to items
    nav = admin.get("/inventory/items").text
    for href in ("/inventory/movements", "/inventory/requests", "/inventory/setup"):
        assert f'href="{href}"' in nav

    r = _post(admin, "/inventory/setup/warehouse", {"code": "", "name": ""})
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(admin, "/inventory/setup/warehouse", {"code": "abj", "name": "Abuja depot"})
    assert r.status_code == 200 and "Abuja depot" in r.text  # type: ignore[attr-defined]
    depot = _one(Warehouse, code="ABJ")
    r = _post(admin, "/inventory/setup/bin", {"warehouse_id": depot, "code": "a1"})
    assert r.status_code == 200 and ">A1<" in r.text  # type: ignore[attr-defined]
    r = _post(admin, "/inventory/setup/unit", {"code": "EA", "name": "Each"})
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(admin, "/inventory/setup/category", {"name": "Spares"})
    assert r.status_code == 422  # type: ignore[attr-defined]


def test_items_movements_and_counts(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", {"year": "2026"})
    general = _one(ItemCategory, name="General")
    each = _one(Unit, code="EA")
    r = _post(admin, "/inventory/items/new", {"name": "", "category_id": general})
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/inventory/items/new",
        {"name": "Toner cartridge", "category_id": general, "unit_id": each, "reorder_level": "5"},
    )
    assert r.status_code == 200 and "ITM-00001" in r.text  # type: ignore[attr-defined]
    item = _one(Item, code="ITM-00001")
    main = _one(Warehouse, code="MAIN")

    lines = {
        "item_id": [item, ""],
        "quantity": ["10", ""],
        "unit_cost": ["4,000", ""],
        "bin_id": ["", ""],
        "to_bin_id": ["", ""],
        "lot_number": ["", ""],
        "expiry": ["", ""],
        "serials": ["", ""],
    }
    r = _post(
        admin,
        "/inventory/movements/new",
        {
            "kind": "adjustment",
            "doc_date": "2026-10-05",
            "warehouse_id": main,
            "reason": "",
            **lines,
            "then": "submit",
        },
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "Say why the stock is being adjusted." in html.unescape(r.text)  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/inventory/movements/new",
        {
            "kind": "adjustment",
            "doc_date": "2026-10-05",
            "warehouse_id": main,
            "reason": "Opening stock",
            **lines,
            "then": "submit",
        },
    )
    assert r.status_code == 200 and "Submitted" in r.text  # type: ignore[attr-defined]
    adjustment = _one(StockDocument, kind="adjustment")
    r = _post(admin, f"/inventory/movements/{adjustment}/post")
    assert r.status_code == 422  # type: ignore[attr-defined]  # the recorder
    _as_if_someone_else(StockDocument, adjustment)
    r = _post(admin, f"/inventory/movements/{adjustment}/post")
    assert r.status_code == 200 and "SA-00001" in r.text  # type: ignore[attr-defined]
    assert "₦40,000.00" in r.text  # type: ignore[attr-defined]

    r = _post(
        admin,
        "/inventory/movements/new",
        {
            "kind": "issue",
            "doc_date": "2026-10-06",
            "warehouse_id": main,
            **{**lines, "quantity": ["12", ""], "unit_cost": ["", ""]},
            "then": "post",
        },
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "Only 10" in html.unescape(r.text)  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/inventory/movements/new",
        {
            "kind": "issue",
            "doc_date": "2026-10-06",
            "warehouse_id": main,
            **{**lines, "quantity": ["6", ""], "unit_cost": ["", ""]},
            "then": "post",
        },
    )
    assert r.status_code == 200 and "SI-00001" in r.text  # type: ignore[attr-defined]
    page = html.unescape(admin.get(f"/inventory/items/{item}").text)
    assert "SA-00001" in page and "SI-00001" in page and "₦16,000.00" in page
    assert "Low stock" in html.unescape(admin.get("/inventory/items").text)

    r = _post(admin, "/inventory/counts", {"warehouse_id": main, "on": "2026-10-07"})
    assert r.status_code == 200 and "SC-00001" in r.text  # type: ignore[attr-defined]
    count = _one(StockCount, number="SC-00001")
    line = _one(StockCountLine, count_id=count)
    r = _post(admin, f"/inventory/counts/{count}/submit", {f"c_{line}": "3"})
    assert r.status_code == 200 and "Submitted" in r.text  # type: ignore[attr-defined]
    r = _post(admin, f"/inventory/counts/{count}/post")
    assert r.status_code == 422  # type: ignore[attr-defined]
    _as_if_someone_else(StockCount, count)
    r = _post(admin, f"/inventory/counts/{count}/post")
    assert r.status_code == 200 and "Posted" in r.text  # type: ignore[attr-defined]
    assert "(₦4,000.00)" in r.text  # type: ignore[attr-defined]  # one short, at cost

    r = _post(
        admin,
        "/inventory/requests/new",
        {"warehouse_id": main, "purpose": "Printing", "item_id": [item], "quantity": ["1"]},
    )
    assert r.status_code == 422  # type: ignore[attr-defined]  # the admin isn't on the staff list
