"""Fixed assets through the browser's eyes (Phase 10e): register an asset, prepare a
depreciation run the preparer can't post, record a revaluation the recorder can't
approve, keep categories and settings, and every fixed asset page."""

import html

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.finance.models import AssetCategory, AssetChange, DepreciationRun, FixedAsset
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)
from tests.web.test_hr_screens import _post

pytestmark = pytest.mark.db


def _category(name: str) -> str:
    with session_scope() as db:
        return str(db.scalars(select(AssetCategory.id).where(AssetCategory.name == name)).one())


def test_register_depreciate_and_revalue(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", year="2026")
    for page in (
        "/finance/assets",
        "/finance/assets?status=disposed",
        "/finance/assets/new",
        "/finance/assets/categories",
        "/finance/assets/categories?edit=new",
        f"/finance/assets/categories?edit={_category('Motor vehicles')}",
        "/finance/depreciation",
    ):
        assert admin.get(page).status_code == 200, page
    assert 'href="/finance/assets"' in admin.get("/finance/journals").text

    r = _post(admin, "/finance/assets/new", name="", category_id="", cost="", acquired_on="")
    assert r.status_code == 422  # type: ignore[attr-defined]
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert "Enter the asset's name." in page and "Choose a category." in page
    r = _post(
        admin,
        "/finance/assets/new",
        name="HP EliteBook",
        category_id=_category("Computer equipment"),
        acquired_on="2026-10-05",
        cost="72,000",
        life_months="36",
        serial="5CG2041XYZ",
    )
    assert r.status_code == 200  # type: ignore[attr-defined]
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert "FA-00001" in page and "below the capitalisation threshold" in page
    with session_scope() as db:
        asset_id = str(db.scalars(select(FixedAsset.id)).one())
    assert admin.get(f"/finance/assets/{asset_id}/edit").status_code == 200
    r = _post(
        admin,
        f"/finance/assets/{asset_id}/edit",
        name="HP EliteBook 840",
        category_id=_category("Computer equipment"),
        acquired_on="2026-10-05",
        cost="720,000",
        life_months="36",
        location="Lagos office",
    )
    assert r.status_code == 200 and "Saved." in r.text  # type: ignore[attr-defined]
    assert "HP EliteBook 840" in r.text  # type: ignore[attr-defined]

    r = _post(admin, "/finance/depreciation/prepare")
    assert r.status_code == 200 and "Prepared" in r.text  # type: ignore[attr-defined]
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert "October 2026" in page and "₦20,000.00" in page
    with session_scope() as db:
        run_id = str(db.scalars(select(DepreciationRun.id)).one())
    r = _post(admin, f"/finance/depreciation/{run_id}/post")
    assert r.status_code == 422  # type: ignore[attr-defined]  # the preparer
    with session_scope() as db:  # as if the nightly task had prepared it
        run = db.get(DepreciationRun, run_id)
        assert run is not None
        run.prepared_by = None
    r = _post(admin, f"/finance/depreciation/{run_id}/post")
    assert r.status_code == 200 and "Posted." in r.text  # type: ignore[attr-defined]
    assert "JV-00001" in r.text  # type: ignore[attr-defined]

    page = admin.get(f"/finance/assets/{asset_id}?record=revaluation").text
    assert 'name="kind" value="revaluation"' in page
    r = _post(admin, f"/finance/assets/{asset_id}/change", kind="revaluation", on="2026-11-02")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(
        admin,
        f"/finance/assets/{asset_id}/change",
        kind="revaluation",
        on="2026-11-02",
        amount="750,000",
        reason="Market value",
    )
    assert r.status_code == 200 and "Recorded" in r.text  # type: ignore[attr-defined]
    with session_scope() as db:
        change_id = str(db.scalars(select(AssetChange.id)).one())
    r = _post(admin, f"/finance/asset-changes/{change_id}/approve", asset_id=asset_id)
    assert r.status_code == 422  # type: ignore[attr-defined]  # the recorder
    r = _post(admin, f"/finance/asset-changes/{change_id}/delete", asset_id=asset_id)
    assert r.status_code == 200 and "deleted" in r.text  # type: ignore[attr-defined]

    listing = html.unescape(admin.get("/finance/assets").text)
    assert "HP EliteBook 840" in listing and "Register and ledger" in listing


def test_categories_and_settings(admin: TestClient) -> None:  # noqa: F811
    r = _post(
        admin,
        "/finance/assets/categories",
        name="Generators",
        method="reducing_balance",
        rate="",
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/finance/assets/settings",
        threshold="250,000",
        reserve_code="3300",
        disposal_code="4910",
        loss_code="6910",
    )
    assert r.status_code == 200 and "250000.00" in r.text  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/finance/assets/settings",
        threshold="1",
        reserve_code="1010",
        disposal_code="4910",
        loss_code="6910",
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
