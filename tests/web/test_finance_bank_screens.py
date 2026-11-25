"""Banking through the browser's eyes (Phase 10d): add a bank account, import a statement,
give a line an entry, reconcile to a nil difference, and record a transfer the recorder
can't approve."""

import html

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.modules.finance.models import (
    Account,
    BankAccount,
    BankReconciliation,
    BankTransfer,
    StatementLine,
)
from tests.conftest import csrf_token
from tests.web.test_finance_screens import admin  # noqa: F401  (fixture)
from tests.web.test_hr_screens import _post

pytestmark = pytest.mark.db

CSV = b"Date,Description,Reference,Debit,Credit\r\n06/10/2026,SMS ALERT CHARGES,,52.50,\r\n"


def _bank(code: str) -> str:
    with session_scope() as db:
        return str(
            db.scalars(
                select(BankAccount.id)
                .join(Account, Account.id == BankAccount.account_id)
                .where(Account.code == code)
            ).one()
        )


def _account(code: str) -> str:
    with session_scope() as db:
        return str(db.scalars(select(Account.id).where(Account.code == code)).one())


def test_banking_reconciliation_and_transfers(admin: TestClient) -> None:  # noqa: F811
    _post(admin, "/finance/periods/years", year="2026")
    main = _bank("1010")
    for page in (
        "/finance/banking",
        "/finance/banking?edit=new",
        f"/finance/banking?edit={main}",
        f"/finance/banking/{main}",
        f"/finance/banking/{main}?tab=matched",
        f"/finance/banking/{main}?tab=entry",
        "/finance/transfers",
    ):
        assert admin.get(page).status_code == 200, page
    nav = admin.get("/finance/banking").text
    for href in ("/finance/banking", "/finance/transfers"):
        assert f'href="{href}"' in nav

    r = _post(admin, "/finance/banking/accounts", name="Zenith · Payroll", account_number="12")
    assert r.status_code == 422  # type: ignore[attr-defined]
    assert "account number" in html.unescape(r.text).lower()  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/finance/banking/accounts",
        name="Zenith · Payroll",
        bank_name="Zenith Bank",
        bank_code="057",
        account_number="1012345678",
        account_name="Adeyemi Holdings Ltd",
        csv_date="Date",
        csv_description="Description",
        csv_reference="Reference",
        csv_money_in="Credit",
        csv_money_out="Debit",
        csv_date_format="%d/%m/%Y",
        csv_skip_rows="0",
    )
    assert r.status_code == 200 and "Zenith · Payroll" in html.unescape(r.text)  # type: ignore[attr-defined]
    payroll = _bank("1011")

    r = admin.post(
        f"/finance/banking/{payroll}/import",
        files={"file": ("oct.csv", CSV, "text/csv")},
        headers={"X-CSRF-Token": csrf_token(admin)},
    )
    assert r.status_code == 200 and "Imported 1 lines" in r.text
    assert "SMS ALERT CHARGES" in r.text
    with session_scope() as db:
        line_id = db.scalars(select(StatementLine.id)).one()
    r = _post(
        admin,
        f"/finance/banking/lines/{line_id}/entry",
        bank_id=payroll,
        account_id="",
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(
        admin,
        f"/finance/banking/lines/{line_id}/entry",
        bank_id=payroll,
        account_id=_first_expense(),
        description="SMS alerts",
    )
    assert r.status_code == 200 and "posts when the reconciliation is approved" in r.text  # type: ignore[attr-defined]

    r = _post(
        admin,
        f"/finance/banking/{payroll}/reconcile",
        statement_date="2026-10-31",
        statement_balance="-52.50",
    )
    assert r.status_code == 200 and "Reconciliation" in r.text  # type: ignore[attr-defined]
    with session_scope() as db:
        rec_id = db.scalars(select(BankReconciliation.id)).one()
    page = html.unescape(r.text)  # type: ignore[attr-defined]
    assert "Entries that post on approval" in page and "SMS alerts" in page
    r = _post(admin, f"/finance/reconciliations/{rec_id}/submit")
    assert r.status_code == 200 and "Submitted" in r.text  # type: ignore[attr-defined]
    r = _post(admin, f"/finance/reconciliations/{rec_id}/approve")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(admin, f"/finance/reconciliations/{rec_id}/send-back")
    assert r.status_code == 200 and "Sent back to draft" in r.text  # type: ignore[attr-defined]

    r = _post(
        admin,
        "/finance/transfers",
        from_id=main,
        to_id=main,
        amount="1000",
        on="2026-10-10",
    )
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(
        admin,
        "/finance/transfers",
        from_id=main,
        to_id=payroll,
        amount="250000",
        on="2026-10-10",
        reference="October salaries",
    )
    assert r.status_code == 200 and "Recorded" in r.text  # type: ignore[attr-defined]
    assert "₦250,000.00" in r.text  # type: ignore[attr-defined]
    with session_scope() as db:
        transfer_id = db.scalars(select(BankTransfer.id)).one()
    r = _post(admin, f"/finance/transfers/{transfer_id}/approve")
    assert r.status_code == 422  # type: ignore[attr-defined]
    r = _post(admin, f"/finance/transfers/{transfer_id}/delete")
    assert r.status_code == 200 and "Deleted" in r.text  # type: ignore[attr-defined]


def _first_expense() -> str:
    with session_scope() as db:
        return str(
            db.scalars(
                select(Account.id)
                .where(Account.type == "expense", Account.is_header.is_(False))
                .order_by(Account.code)
            ).first()
        )
