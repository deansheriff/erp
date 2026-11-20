"""Expenses (ADR 0020): drafting with receipts, the approval chain tiered by amount,
corrections and returns, limit warnings, payment with separation of duties, cash
advances and repayments, and the journals queued for the ledger."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files.models import StoredFile
from app.modules.expenses import service, settings
from app.modules.expenses.models import (
    ExpenseDocument,
    ExpenseJournal,
    ExpenseJournalLine,
    ExpenseLine,
)
from tests.expenses.conftest import JPEG, Team

pytestmark = pytest.mark.db


def _approve(team: Team, document: object, *who: str) -> None:
    for name in who:
        service.decide(team.hr.db, team.hr.actor(name), document, decision="approve")  # type: ignore[arg-type]


def _pay(team: Team, document: object, who: str = "Hauwa") -> None:
    service.pay(
        team.hr.db,
        team.hr.actor(who),
        [document],  # type: ignore[list-item]
        paid_on=clock.today(),
        method="transfer",
        reference="GTB-00123",
    )


def _journals(team: Team, document: object) -> list[list[tuple[str, Decimal, Decimal]]]:
    db = team.hr.db
    result = []
    for journal in db.scalars(
        select(ExpenseJournal)
        .where(ExpenseJournal.document_id == document)
        .order_by(ExpenseJournal.created_at)
    ):
        lines = db.scalars(
            select(ExpenseJournalLine).where(ExpenseJournalLine.journal_id == journal.id)
        )
        result.append(sorted((line.account, line.debit, line.credit) for line in lines))
    return result


# ------------------------------------------------------------------ drafting


def test_a_claim_needs_lines_and_receipts_before_it_goes(team: Team) -> None:
    db, efe = team.hr.db, team.hr.actor("Efe")
    claim = service.create(db, efe, kind="claim", title="Client visit, Abuja")
    doc = db.get(ExpenseDocument, claim)
    assert doc is not None and doc.number == "EXP-00001" and doc.status == "draft"
    with pytest.raises(ValidationFailed, match=""):
        service.submit(db, efe, claim)  # type: ignore[arg-type]
    line = service.save_line(
        db,
        efe,
        claim,  # type: ignore[arg-type]
        None,
        day=clock.today(),
        category_id=team.category("Meals").id,
        description="Lunch with client",
        cost_centre_id=None,
        amount="12,500",
    )
    with pytest.raises(ValidationFailed) as exc:
        service.submit(db, efe, claim)  # type: ignore[arg-type]
    assert "receipt" in exc.value.field_errors["form"]
    service.attach_receipt(
        db, efe, line, filename="lunch.jpg", declared_mime="image/jpeg", data=JPEG
    )
    assert db.get(ExpenseLine, line).receipt_file_id is not None  # type: ignore[union-attr]
    service.submit(db, efe, claim)  # type: ignore[arg-type]
    assert doc.status == "submitted" and doc.total == Decimal("12500.00")
    with pytest.raises(Conflict):
        service.save_line(
            db,
            efe,
            claim,  # type: ignore[arg-type]
            None,
            day=clock.today(),
            category_id=team.category("Meals").id,
            description="More",
            cost_centre_id=None,
            amount="100",
        )
    # Receipts are seen by those who can see the claim.
    receipt = db.get(ExpenseLine, line).receipt_file_id  # type: ignore[union-attr]
    assert db.get(StoredFile, receipt).classification == "directory"  # type: ignore[union-attr]


def test_amounts_and_dates_are_checked(team: Team) -> None:
    db, efe = team.hr.db, team.hr.actor("Efe")
    claim = service.create(db, efe, kind="claim", title="Trip")
    for amount, problem in (("abc", "amount"), ("0", "amount"), ("10.555", "amount")):
        with pytest.raises(ValidationFailed) as exc:
            service.save_line(
                db,
                efe,
                claim,  # type: ignore[arg-type]
                None,
                day=clock.today(),
                category_id=team.category("Travel").id,
                description="Taxi",
                cost_centre_id=None,
                amount=amount,
            )
        assert problem in exc.value.field_errors
    with pytest.raises(ValidationFailed) as exc:
        service.save_line(
            db,
            efe,
            claim,  # type: ignore[arg-type]
            None,
            day=None,
            category_id=None,
            description="",
            cost_centre_id=None,
            amount="10",
        )
    assert {"day", "category_id", "description"} <= set(exc.value.field_errors)


# ------------------------------------------------------------------ the approval chain


@pytest.mark.parametrize(
    ("amount", "steps"),
    [
        ("30000", ["manager"]),
        ("150000", ["manager", "second"]),
        ("600000", ["manager", "second", "finance"]),
    ],
)
def test_the_chain_grows_with_the_amount(team: Team, amount: str, steps: list[str]) -> None:
    db = team.hr.db
    claim = team.claim("Efe", amount)
    service.submit(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    view = service.get(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    assert [s.kind for s in view.steps] == steps
    assert view.steps[0].approver == "Gbenga Test"
    if len(steps) > 1:
        assert view.steps[1].approver == "Bola Test"
    # Bola manages Efe too, but the first step is Gbenga's.
    with pytest.raises(PermissionDenied, match="someone else"):
        service.decide(db, team.hr.actor("Bola"), claim, decision="approve")  # type: ignore[arg-type]
    deciders = {"manager": "Gbenga", "second": "Bola", "finance": "Ada"}
    for kind in steps:
        assert service.get(db, team.hr.actor("Efe"), claim).status == "submitted"  # type: ignore[arg-type]
        _approve(team, claim, deciders[kind])
    assert service.get(db, team.hr.actor("Efe"), claim).status == "approved"  # type: ignore[arg-type]


def test_inbox_shows_each_approver_their_step(team: Team) -> None:
    db = team.hr.db
    claim = team.claim("Efe", "600000")
    service.submit(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    assert [d.id for d in service.inbox(db, team.hr.actor("Gbenga"))] == [claim]
    assert service.inbox(db, team.hr.actor("Bola")) == []
    # Ada (Finance Manager) can step in on any manager step.
    assert [d.id for d in service.inbox(db, team.hr.actor("Ada"))] == [claim]
    _approve(team, claim, "Gbenga")
    assert [d.id for d in service.inbox(db, team.hr.actor("Bola"))] == [claim]
    assert service.inbox(db, team.hr.actor("Gbenga")) == []
    assert [d.id for d in service.decided_by_me(db, team.hr.actor("Gbenga"))] == [claim]


def test_nobody_approves_their_own_claim(team: Team) -> None:
    db = team.hr.db
    claim = team.claim("Ada", "5000")  # Ada has no manager: any approver decides
    service.submit(db, team.hr.actor("Ada"), claim)  # type: ignore[arg-type]
    view = service.get(db, team.hr.actor("Ada"), claim)  # type: ignore[arg-type]
    assert view.steps[0].approver is None and not view.can_decide
    with pytest.raises(PermissionDenied, match="your own"):
        service.decide(db, team.hr.actor("Ada"), claim, decision="approve")  # type: ignore[arg-type]


def test_approver_corrections_are_kept_and_returns_need_a_note(team: Team) -> None:
    db = team.hr.db
    claim = team.claim("Efe", "25000", "4000")
    service.submit(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    lines = {
        line.amount: line
        for line in db.scalars(select(ExpenseLine).where(ExpenseLine.document_id == claim))
    }
    with pytest.raises(ValidationFailed):
        service.decide(db, team.hr.actor("Gbenga"), claim, decision="return", note="no")  # type: ignore[arg-type]
    service.decide(
        db,
        team.hr.actor("Gbenga"),
        claim,  # type: ignore[arg-type]
        decision="return",
        note="Split the hotel from the taxi",
        corrections={lines[Decimal("25000.00")].id: "20,000"},
    )
    doc = db.get(ExpenseDocument, claim)
    assert doc is not None and doc.status == "returned" and doc.total == Decimal("24000.00")
    corrected = lines[Decimal("25000.00")]
    assert (corrected.amount, corrected.original_amount) == (
        Decimal("20000.00"),
        Decimal("25000.00"),
    )
    view = service.get(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    assert view.can_edit and view.steps[0].note == "Split the hotel from the taxi"
    service.submit(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    view = service.get(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    assert [s.status for s in view.steps] == ["pending"]  # a fresh chain
    service.decide(
        db, team.hr.actor("Gbenga"), claim, decision="reject", note="Not a business cost"
    )  # type: ignore[arg-type]
    assert doc.status == "rejected"


def test_limits_warn_but_do_not_block(team: Team) -> None:
    db = team.hr.db
    meals = team.category("Meals")
    settings.save_category(
        db,
        team.hr.actor("Ada"),
        meals.id,
        name="Meals",
        gl_account=meals.gl_account,
        claim_limit="20000",
        receipt_above="0",
        is_active=True,
    )
    claim = team.claim("Efe", "26000", "30000", category="Meals")
    view = service.get(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    text = " ".join(view.flags)
    assert "Meals is ₦56,000.00, over its ₦20,000.00 limit per claim" in text
    assert "weekly limit" in text and "₦50,000.00" in text
    service.submit(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    assert service.get(db, team.hr.actor("Gbenga"), claim).flags  # type: ignore[arg-type]
    settings.save_person_limit(
        db, team.hr.actor("Ada"), team.hr.employee("Efe"), weekly_limit="100000", monthly_limit=""
    )
    flags = service.get(db, team.hr.actor("Gbenga"), claim).flags  # type: ignore[arg-type]
    assert not any("weekly" in f for f in flags)


def test_people_see_only_claims_in_scope(team: Team) -> None:
    db = team.hr.db
    claim = team.claim("Efe", "1000")
    for who in ("Femi", "Dayo"):
        with pytest.raises(NotFound):
            service.get(db, team.hr.actor(who), claim)  # type: ignore[arg-type]
    assert service.get(db, team.hr.actor("Bola"), claim).number  # type: ignore[arg-type]
    with pytest.raises(PermissionDenied):
        service.submit(db, team.hr.actor("Bola"), claim)  # type: ignore[arg-type]


# ------------------------------------------------------------------ paying


def test_payment_queues_a_balanced_journal(team: Team) -> None:
    db = team.hr.db
    claim = team.claim("Efe", "12000", "3000.50")
    service.submit(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    with pytest.raises(Conflict):
        _pay(team, claim)
    _approve(team, claim, "Gbenga")
    with pytest.raises(ValidationFailed) as exc:
        service.pay(
            db,
            team.hr.actor("Hauwa"),
            [claim],
            paid_on=clock.today(),
            method="transfer",
            reference="",
        )  # type: ignore[list-item]
    assert "reference" in exc.value.field_errors
    _pay(team, claim)
    doc = db.get(ExpenseDocument, claim)
    assert doc is not None and doc.status == "reimbursed" and doc.paid_amount == Decimal("15000.50")
    assert _journals(team, claim) == [
        sorted(
            [
                ("6100", Decimal("15000.50"), Decimal("0.00")),
                ("1010", Decimal("0.00"), Decimal("15000.50")),
            ]
        )
    ]
    assert [j.memo for j in service.queued_journals(db)] == [f"{doc.number} Trip"]


def test_approvers_and_claimants_cannot_pay(team: Team) -> None:
    db = team.hr.db
    from app.identity.models import User

    gbenga = db.get(User, team.hr.org.users["Gbenga"])
    assert gbenga is not None
    team.hr.people.give(gbenga, "reimburser")
    claim = team.claim("Efe", "5000")
    service.submit(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    _approve(team, claim, "Gbenga")
    with pytest.raises(PermissionDenied, match="approved"):
        _pay(team, claim, "Gbenga")
    own = team.claim("Hauwa", "5000")
    service.submit(db, team.hr.actor("Hauwa"), own)  # type: ignore[arg-type]
    _approve(team, own, "Ada")
    with pytest.raises(PermissionDenied, match="own"):
        _pay(team, own, "Hauwa")
    team.hr.people.give(gbenga, "organization_administrator")  # holds the override
    _pay(team, claim, "Gbenga")


def test_advance_is_used_by_claims_then_repaid(team: Team) -> None:
    db, efe = team.hr.db, team.hr.actor("Efe")
    advance = service.create(db, efe, kind="advance", title="Site trip float", amount="50000")
    assert db.get(ExpenseDocument, advance).number == "ADV-00001"  # type: ignore[union-attr]
    service.submit(db, efe, advance)  # type: ignore[arg-type]
    _approve(team, advance, "Gbenga")
    _pay(team, advance)
    assert _journals(team, advance) == [
        sorted(
            [
                ("1410", Decimal("50000.00"), Decimal("0.00")),
                ("1010", Decimal("0.00"), Decimal("50000.00")),
            ]
        )
    ]
    assert [a.id for a in service.open_advances(db, team.hr.employee("Efe"))] == [advance]

    claim = service.create(db, efe, kind="claim", title="Site trip", advance_id=advance)  # type: ignore[arg-type]
    service.save_line(
        db,
        efe,
        claim,  # type: ignore[arg-type]
        None,
        day=clock.today(),
        category_id=team.category("Fuel").id,
        description="Diesel",
        cost_centre_id=team.cost_centre,  # type: ignore[arg-type]
        amount="32000",
        no_receipt_reason="Pump receipt faded",
    )
    service.submit(db, efe, claim)  # type: ignore[arg-type]
    _approve(team, claim, "Gbenga")
    _pay(team, claim)
    doc = db.get(ExpenseDocument, claim)
    assert doc is not None and (doc.advance_applied, doc.paid_amount) == (
        Decimal("32000.00"),
        Decimal("0.00"),
    )
    assert _journals(team, claim) == [
        sorted(
            [
                ("6130", Decimal("32000.00"), Decimal("0.00")),
                ("1410", Decimal("0.00"), Decimal("32000.00")),
            ]
        )
    ]
    view = service.get(db, efe, advance)  # type: ignore[arg-type]
    assert view.outstanding == Decimal("18000.00") and not view.settled
    assert [d.id for d in service.repayments_due(db, team.hr.actor("Hauwa"))] == [advance]
    with pytest.raises(ValidationFailed) as exc:
        service.record_repayment(
            db,
            team.hr.actor("Hauwa"),
            advance,
            amount="20000",
            received_on=clock.today(),
            method="cash",
            reference="",  # type: ignore[arg-type]
        )
    assert "₦18,000.00" in exc.value.field_errors["repay_amount"]
    service.record_repayment(
        db,
        team.hr.actor("Hauwa"),
        advance,
        amount="18000",
        received_on=clock.today(),
        method="cash",
        reference="Receipt 7",  # type: ignore[arg-type]
    )
    view = service.get(db, efe, advance)  # type: ignore[arg-type]
    assert view.outstanding == Decimal("0.00") and view.settled
    assert service.open_advances(db, team.hr.employee("Efe")) == []


def test_a_claim_bigger_than_its_advance_is_paid_the_rest(team: Team) -> None:
    db, efe = team.hr.db, team.hr.actor("Efe")
    advance = service.create(db, efe, kind="advance", title="Float", amount="20000")
    service.submit(db, efe, advance)  # type: ignore[arg-type]
    _approve(team, advance, "Gbenga")
    _pay(team, advance)
    claim = team.claim("Efe", "45000")
    service.update_header(db, efe, claim, title="Trip", advance_id=advance)  # type: ignore[arg-type]
    service.submit(db, efe, claim)  # type: ignore[arg-type]
    _approve(team, claim, "Gbenga")
    _pay(team, claim)
    doc = db.get(ExpenseDocument, claim)
    assert doc is not None and (doc.advance_applied, doc.paid_amount) == (
        Decimal("20000.00"),
        Decimal("25000.00"),
    )
    assert service.get(db, efe, advance).settled  # type: ignore[arg-type]


def test_cancelling_and_editing_rules(team: Team) -> None:
    db = team.hr.db
    claim = team.claim("Efe", "1000")
    service.submit(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    with pytest.raises(PermissionDenied):
        service.update_header(db, team.hr.actor("Gbenga"), claim, title="Mine now")  # type: ignore[arg-type]
    service.cancel(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]
    assert service.inbox(db, team.hr.actor("Gbenga")) == []
    with pytest.raises(Conflict):
        service.cancel(db, team.hr.actor("Efe"), claim)  # type: ignore[arg-type]


def test_settings_are_validated(team: Team) -> None:
    db, ada = team.hr.db, team.hr.actor("Ada")
    base = {
        "second_level_above": "100000",
        "finance_above": "500000",
        "weekly_limit": "",
        "monthly_limit": "",
        "advances_account": "1410",
        "bank_account": "1010",
    }
    with pytest.raises(ValidationFailed) as exc:
        settings.save_settings(db, ada, **{**base, "finance_above": "50000"})
    assert "finance_above" in exc.value.field_errors
    with pytest.raises(PermissionDenied):
        settings.save_settings(db, team.hr.actor("Efe"), **base)
    settings.save_settings(db, ada, **base)
    assert settings.get_settings(db).weekly_limit is None
    with pytest.raises(ValidationFailed) as exc:
        settings.save_category(
            db,
            ada,
            None,
            name="meals",
            gl_account="6120",
            claim_limit="",
            receipt_above="0",
            is_active=True,
        )
    assert "name" in exc.value.field_errors
