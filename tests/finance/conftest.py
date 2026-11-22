"""Finance fixtures: the HR staff with finance roles. Ada and Bola are Finance Managers
(draft, post, manage periods), Chi is a Finance Officer (drafts only). The financial year
2026 is open."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.identity.models import User
from app.modules.finance import journals, periods
from app.modules.finance.models import Account
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401


@dataclass
class Finance:
    hr: Hr
    year_id: uuid.UUID

    @property
    def db(self):  # type: ignore[no-untyped-def]
        return self.hr.db

    def account(self, code: str) -> Account:
        return self.hr.db.scalars(select(Account).where(Account.code == code)).one()

    def line(self, code: str, debit: str = "", credit: str = "") -> journals.LineInput:
        return journals.LineInput(str(self.account(code).id), "", "", debit, credit)

    def posted(
        self, day: date, lines: list[tuple[str, str, str]], memo: str = "Test journal"
    ) -> uuid.UUID:
        """Ada drafts, Bola posts."""
        draft = journals.save_draft(
            self.db,
            self.hr.actor("Ada"),
            None,
            journal_date=day,
            memo=memo,
            lines=[self.line(code, d, c) for code, d, c in lines],
        )
        journals.post(self.db, self.hr.actor("Bola"), draft)
        return draft


@pytest.fixture()
def finance(hr: Hr) -> Finance:  # noqa: F811
    db = hr.db
    for who, template in (
        ("Ada", "finance_manager"),
        ("Bola", "finance_manager"),
        ("Chi", "finance_officer"),
    ):
        user = db.get(User, hr.org.users[who])
        assert user is not None
        hr.people.give(user, template)
    year_id = periods.open_year(db, hr.actor("Ada"), 2026)
    return Finance(hr, year_id)


def money(value: str) -> Decimal:
    return Decimal(value)
