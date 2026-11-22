"""Finance through the browser's eyes (Phase 10a): open a year, draft a journal that the
drafter can't post, the chart of accounts, recurring journals and every report."""

import html

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.finance.models import Account, Journal
from tests.conftest import Installed, csrf_token, sign_in
from tests.web.test_hr_screens import _post

pytestmark = pytest.mark.db


@pytest.fixture()
def admin(installed: Installed, client: TestClient) -> TestClient:
    sign_in(client, installed.admin_email, installed.admin_password, installed.admin_secret)
    return client


def _account(code: str) -> str:
    with session_scope() as db:
        return str(db.scalars(select(Account.id).where(Account.code == code)).one())


def _lines(*rows: tuple[str, str, str]) -> dict[str, list[str]]:
    return {
        "account_id": [_account(code) for code, _, _ in rows],
        "cost_centre_id": ["" for _ in rows],
        "description": ["" for _ in rows],
        "debit": [d for _, d, _ in rows],
        "credit": [c for _, _, c in rows],
    }


def test_every_finance_page_renders(admin: TestClient) -> None:
    for page in (
        "/finance",
        "/finance/journals?tab=posted",
        "/finance/journals?tab=held",
        "/finance/journals/new",
        "/finance/accounts",
        "/finance/accounts?edit=new",
        "/finance/recurring",
        "/finance/recurring?edit=new",
        "/finance/periods",
        "/finance/reports",
        "/finance/reports?report=pl",
        "/finance/reports?report=bs",
        "/finance/reports?report=cf",
        "/finance/reports?report=gl",
    ):
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/finance/journals").text
    for href in ("/finance/accounts", "/finance/recurring", "/finance/periods", "/finance/reports"):
        assert f'href="{href}"' in nav
    assert "1010" in admin.get("/finance/accounts").text


def test_a_draft_is_posted_by_someone_else(admin: TestClient) -> None:
    assert "No financial year yet" in admin.get("/finance/periods").text
    r = _post(admin, "/finance/periods/years", year="2026")
    assert r.status_code == 200 and "The year is open." in r.text  # type: ignore[attr-defined]

    r = admin.post(
        "/finance/journals",
        data={
            "journal_date": "2026-04-01",
            "memo": "",
            "csrf_token": csrf_token(admin),
            **_lines(("6400", "500,000", ""), ("1010", "", "500,000")),
        },
    )
    assert r.status_code == 422 and "Say what the journal is for." in html.unescape(r.text)
    r = admin.post(
        "/finance/journals",
        data={
            "journal_date": "2026-04-01",
            "memo": "Office rent",
            "csrf_token": csrf_token(admin),
            **_lines(("6400", "500,000", ""), ("1010", "", "500,000")),
        },
    )
    page = html.unescape(r.text)
    assert r.status_code == 200 and "Draft saved" in page and "Office rent" in page
    assert "You drafted this journal, so someone else posts it." in page
    with session_scope() as db:
        journal_id = db.scalars(select(Journal.id)).one()
    r = _post(admin, f"/finance/journals/{journal_id}/post")
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "1 draft" in admin.get("/finance/periods").text
    assert "Office rent" in admin.get("/finance/journals").text


def test_chart_recurring_and_csv(admin: TestClient) -> None:
    r = _post(admin, "/finance/accounts", code="6600", name="", type="expense")
    assert r.status_code == 422 and "Enter a name." in r.text  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/finance/accounts",
        code="6600",
        name="Security",
        type="expense",
        parent_id=_account("62"),
    )
    assert r.status_code == 200 and "Security" in r.text  # type: ignore[attr-defined]

    r = admin.post(
        "/finance/recurring",
        data={
            "name": "Rent",
            "memo": "Monthly rent accrual",
            "day_of_month": "1",
            "first_month": "2026-11",
            "csrf_token": csrf_token(admin),
            **_lines(("6400", "850,000", ""), ("2020", "", "850,000")),
        },
    )
    assert r.status_code == 200 and "Monthly rent accrual" in r.text

    r = admin.get("/finance/reports/trial-balance.csv?as_at=2026-12-31")
    assert r.status_code == 200 and r.text.startswith("Code,Account,Debit,Credit")
