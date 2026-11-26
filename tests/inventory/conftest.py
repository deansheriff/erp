"""Inventory fixtures: the HR staff with inventory roles. Ada and Bola are Inventory
Managers (Ada also a Finance Manager, to open 2026), Hauwa is an Inventory Clerk, and Chi
is Dayo's line manager. A second warehouse, Abuja depot, has bin A1."""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.identity.models import User
from app.modules.finance import periods
from app.modules.finance.models import Account
from app.modules.inventory import items
from app.modules.inventory.items import ItemInput
from app.modules.inventory.models import ItemCategory, Unit, Warehouse
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401


@dataclass
class Stock:
    hr: Hr
    main: uuid.UUID
    depot: uuid.UUID
    bin_a1: uuid.UUID

    @property
    def db(self):  # type: ignore[no-untyped-def]
        return self.hr.db

    def actor(self, who: str):  # type: ignore[no-untyped-def]
        return self.hr.actor(who)

    def account(self, code: str) -> Account:
        return self.db.scalars(select(Account).where(Account.code == code)).one()

    def item(
        self, name: str, *, valuation: str = "average", tracking: str = "none", **extra: str
    ) -> uuid.UUID:
        general = self.db.scalars(select(ItemCategory)).one()
        each = self.db.scalars(select(Unit).where(Unit.code == "EA")).one()
        return items.save_item(
            self.db,
            self.actor("Ada"),
            None,
            ItemInput(
                code=extra.pop("code", ""),
                name=name,
                category_id=str(general.id),
                unit_id=str(each.id),
                valuation=valuation,
                tracking=tracking,
                **extra,
            ),
        )


@pytest.fixture()
def stock(hr: Hr) -> Stock:  # noqa: F811
    db = hr.db
    for who, templates in (
        ("Ada", ("inventory_manager", "finance_manager")),
        ("Bola", ("inventory_manager",)),
        ("Hauwa", ("inventory_clerk",)),
        ("Chi", ("line_manager",)),
    ):
        user = db.get(User, hr.org.users[who])
        assert user is not None
        for template in templates:
            hr.people.give(user, template)
    periods.open_year(db, hr.actor("Ada"), 2026)
    main = db.scalars(select(Warehouse).where(Warehouse.code == "MAIN")).one().id
    depot = items.save_warehouse(
        db, hr.actor("Ada"), None, code="ABJ", name="Abuja depot", address="", active=True
    )
    bin_a1 = items.add_bin(db, hr.actor("Ada"), depot, "A1")
    return Stock(hr, main, depot, bin_a1)
