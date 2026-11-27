"""Procurement fixtures. Dayo (HR Officer) reports to Chi, who is a Line Manager here;
Hauwa has no position, so no line manager. Bola is a Finance Manager, Ada a Director and
Inventory Manager, and Gbenga the Procurement Officer. Three suppliers, one with an email
address, and a stock item."""

import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from app.identity.models import User
from app.modules.finance import periods, suppliers
from app.modules.finance.models import Account, WhtType
from app.modules.finance.suppliers import SupplierInput
from app.modules.inventory import items
from app.modules.inventory.items import ItemInput
from app.modules.inventory.models import ItemCategory, Unit, Warehouse
from tests.authz.conftest import People, people  # noqa: F401  (re-exported fixtures)
from tests.hr.conftest import Hr, hr  # noqa: F401
from tests.org.conftest import Org, org  # noqa: F401


@dataclass
class Buying:
    hr: Hr
    main: uuid.UUID
    rice: uuid.UUID
    suppliers: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def db(self):  # type: ignore[no-untyped-def]
        return self.hr.db

    def actor(self, who: str):  # type: ignore[no-untyped-def]
        return self.hr.actor(who)

    def account(self, code: str) -> Account:
        return self.db.scalars(select(Account).where(Account.code == code)).one()


def _supplier(db, actor, name: str, email: str) -> uuid.UUID:  # type: ignore[no-untyped-def]
    wht = db.scalars(select(WhtType).where(WhtType.key == "supply_of_goods")).one()
    return suppliers.save(
        db,
        actor,
        None,
        SupplierInput(name, "company", email, "", "", "", "30", str(wht.id), "", "", "", "", True),
    )


@pytest.fixture()
def buying(hr: Hr) -> Buying:  # noqa: F811
    db = hr.db
    for who, templates in (
        ("Chi", ("line_manager",)),
        ("Bola", ("finance_manager",)),
        ("Ada", ("director", "inventory_manager")),
        ("Gbenga", ("procurement_officer",)),
    ):
        user = db.get(User, hr.org.users[who])
        assert user is not None
        for template in templates:
            hr.people.give(user, template)
    periods.open_year(db, hr.actor("Bola"), 2026)
    general = db.scalars(select(ItemCategory)).one()
    each = db.scalars(select(Unit).where(Unit.code == "EA")).one()
    rice = items.save_item(
        db,
        hr.actor("Ada"),
        None,
        ItemInput(
            code="",
            name="Rice 50kg bag",
            category_id=str(general.id),
            unit_id=str(each.id),
            valuation="average",
            tracking="none",
        ),
    )
    main = db.scalars(select(Warehouse).where(Warehouse.code == "MAIN")).one().id
    out = Buying(hr, main, rice)
    for name, email in (
        ("Kano Grains Ltd", "sales@kanograins.example"),
        ("Lagos Office Supplies Ltd", ""),
        ("Abuja Traders", ""),
    ):
        out.suppliers[name] = _supplier(db, hr.actor("Bola"), name, email)
    return out
