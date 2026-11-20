"""Payroll and the ledger (ADR 0022): bank codes for the bank file, the account codes
journals use, and the balanced journals queued for the ledger (Phase 10) when a run is
posted or paid and when a loan is approved."""

import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import engine
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.payroll.models import (
    PayrollAccounts,
    PayrollBank,
    PayrollJournal,
    PayrollJournalLine,
    PayrollRun,
    Payslip,
    StaffLoan,
)
from app.org import service as org

MANAGE = "payroll.settings.manage"
ZERO = Decimal(0)
ACCOUNT_FIELDS = (
    ("salaries", "Salaries and wages"),
    ("employer_pension", "Employer pension"),
    ("employer_nsitf", "Employer NSITF"),
    ("paye_payable", "PAYE payable"),
    ("pension_payable", "Pension payable"),
    ("nhf_payable", "NHF payable"),
    ("nhis_payable", "NHIS payable"),
    ("nsitf_payable", "NSITF payable"),
    ("deductions_payable", "Other deductions payable"),
    ("net_pay_payable", "Net salaries payable"),
    ("staff_loans", "Staff loans"),
    ("bank", "Bank"),
)
ACCOUNT_CODE = re.compile(r"[A-Za-z0-9.\-]{1,20}")
BANK_CODE = re.compile(r"\d{3,6}")
SUFFIXES = ("plc", "limited", "ltd", "bank", "nigeria")

JournalLine = tuple[str, uuid.UUID | None, Decimal, Decimal]  # account, cost centre, Dr, Cr


def _require(db: Session, actor: Actor) -> None:
    if not engine.check(db, actor.user_id, MANAGE):
        raise PermissionDenied()


# ------------------------------------------------------------------ account codes


def accounts(db: Session) -> PayrollAccounts:
    row = db.get(PayrollAccounts, 1)
    if row is None:  # seeded by migration 0011
        raise Conflict("Payroll ledger accounts are missing.")
    return row


def save_accounts(db: Session, actor: Actor, values: dict[str, str]) -> None:
    _require(db, actor)
    row = accounts(db)
    errors: dict[str, str] = {}
    clean: dict[str, str] = {}
    for name, _ in ACCOUNT_FIELDS:
        value = values.get(name, "").strip()
        if not ACCOUNT_CODE.fullmatch(value):
            errors[name] = "Enter an account code: letters, digits, dots or hyphens."
        clean[name] = value
    if errors:
        raise ValidationFailed(field_errors=errors)
    before = {name: getattr(row, name) for name, _ in ACCOUNT_FIELDS}
    for name, value in clean.items():
        setattr(row, name, value)
    db.flush()
    record_by(
        db,
        actor,
        "payroll.accounts.update",
        entity_type="payroll_accounts",
        entity_id="1",
        changes=diff_changes(before, clean),
    )


# ------------------------------------------------------------------ bank codes


def _key(name: str) -> str:
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    return "".join(words)


def list_banks(db: Session) -> list[PayrollBank]:
    return list(db.scalars(select(PayrollBank).order_by(PayrollBank.name)))


def save_bank(
    db: Session,
    actor: Actor,
    bank_id: uuid.UUID | None,
    *,
    name: str,
    code: str,
    other_names: str,
    is_active: bool = True,
) -> uuid.UUID:
    _require(db, actor)
    name, code = name.strip(), code.strip()
    aliases = ", ".join(a.strip() for a in other_names.split(",") if a.strip())
    errors: dict[str, str] = {}
    if not name:
        errors["bank_name"] = "Enter the bank's name."
    if not BANK_CODE.fullmatch(code):
        errors["bank_code"] = "Enter the CBN code: 3 to 6 digits."
    if errors:
        raise ValidationFailed(field_errors=errors)
    clash = db.scalars(
        select(PayrollBank).where(PayrollBank.name == name, PayrollBank.id != bank_id)
    ).first()
    if clash:
        raise ValidationFailed(field_errors={"bank_name": "That bank is already on the list."})
    if bank_id:
        row = db.get(PayrollBank, bank_id)
        if row is None:
            raise NotFound()
        before = {"name": row.name, "code": row.code, "other_names": row.other_names}
    else:
        row, before = PayrollBank(name=name, code=code), {}
        db.add(row)
    row.name, row.code, row.other_names, row.is_active = name, code, aliases or None, is_active
    db.flush()
    record_by(
        db,
        actor,
        "payroll.bank.save",
        entity_type="payroll_bank",
        entity_id=str(row.id),
        changes=diff_changes(before, {"name": name, "code": code, "other_names": aliases}),
    )
    return row.id


def bank_matcher(db: Session) -> Callable[[str | None], PayrollBank | None]:
    """Find the listed bank for a bank name typed on an HR record, by its name or any of
    its other names, ignoring case, spacing and endings like "Plc"."""
    keys: dict[str, PayrollBank] = {}
    for bank in db.scalars(select(PayrollBank).where(PayrollBank.is_active)):
        for alias in [bank.name, *(bank.other_names or "").split(",")]:
            if alias.strip():
                keys.setdefault(_key(alias), bank)

    def match(name: str | None) -> PayrollBank | None:
        return keys.get(_key(name)) if name else None

    return match


# ------------------------------------------------------------------ journals


@dataclass(frozen=True, slots=True)
class JournalView:
    kind: str
    journal_date: date
    memo: str
    status: str
    lines: list[tuple[str, str | None, Decimal, Decimal]]  # account, cost centre, Dr, Cr
    debit: Decimal
    credit: Decimal


def _queue(
    db: Session,
    *,
    kind: str,
    when: date,
    memo: str,
    lines: list[JournalLine],
    run_id: uuid.UUID | None = None,
    loan_id: uuid.UUID | None = None,
) -> None:
    merged: dict[tuple[str, uuid.UUID | None], Decimal] = defaultdict(lambda: ZERO)
    for account, cost_centre, debit, credit in lines:
        merged[(account, cost_centre)] += debit - credit
    debits = sum((v for v in merged.values() if v > 0), ZERO)
    credits = -sum((v for v in merged.values() if v < 0), ZERO)
    assert debits == credits, "unbalanced journal"  # noqa: S101
    journal = PayrollJournal(
        run_id=run_id, loan_id=loan_id, kind=kind, journal_date=when, memo=memo[:200]
    )
    db.add(journal)
    db.flush()
    for (account, cost_centre), net in merged.items():
        if net:
            db.add(
                PayrollJournalLine(
                    journal_id=journal.id,
                    account=account,
                    cost_centre_id=cost_centre,
                    debit=net if net > 0 else ZERO,
                    credit=-net if net < 0 else ZERO,
                )
            )
    db.flush()


def queue_accrual(
    db: Session,
    run: PayrollRun,
    slips: list[Payslip],
    loan_repayments: dict[uuid.UUID, Decimal],  # by payslip
    *,
    when: date,
    memo: str,
) -> None:
    """Posting a run: costs by cost centre against what is owed to the tax office, PFAs,
    FMBN, other payees, the staff loan book and the employees."""
    acc = accounts(db)
    centres = {s.employee_id: org.cost_centre_of(db, s.employee_id) for s in slips}
    lines: list[JournalLine] = []
    for s in slips:
        centre = centres.get(s.employee_id)
        loans = loan_repayments.get(s.id, ZERO)
        lines += [
            (acc.salaries, centre, s.gross, ZERO),
            (acc.employer_pension, centre, s.pension_employer, ZERO),
            (acc.employer_nsitf, centre, s.nsitf, ZERO),
            (acc.paye_payable, None, ZERO, s.paye),
            (acc.pension_payable, None, ZERO, s.pension_employee + s.pension_employer),
            (acc.nhf_payable, None, ZERO, s.nhf),
            (acc.nhis_payable, None, ZERO, s.nhis),
            (acc.nsitf_payable, None, ZERO, s.nsitf),
            (acc.deductions_payable, None, ZERO, s.other_deductions - loans),
            (acc.staff_loans, None, ZERO, loans),
            (acc.net_pay_payable, None, ZERO, s.net),
        ]
    _queue(db, kind="accrual", when=when, memo=memo, lines=lines, run_id=run.id)


def queue_payment(db: Session, run: PayrollRun, net: Decimal, *, when: date, memo: str) -> None:
    acc = accounts(db)
    lines: list[JournalLine] = [(acc.net_pay_payable, None, net, ZERO), (acc.bank, None, ZERO, net)]
    _queue(db, kind="payment", when=when, memo=memo, lines=lines, run_id=run.id)


def queue_loan(db: Session, loan: StaffLoan, *, when: date, memo: str) -> None:
    acc = accounts(db)
    lines: list[JournalLine] = [
        (acc.staff_loans, None, loan.amount, ZERO),
        (acc.bank, None, ZERO, loan.amount),
    ]
    _queue(db, kind="loan", when=when, memo=memo, lines=lines, loan_id=loan.id)


def journals_of_run(db: Session, run_id: uuid.UUID) -> list[JournalView]:
    labels = dict(org.cost_centre_choices(db))
    views = []
    for j in db.scalars(
        select(PayrollJournal)
        .where(PayrollJournal.run_id == run_id)
        .order_by(PayrollJournal.created_at)
    ):
        rows = list(
            db.scalars(
                select(PayrollJournalLine)
                .where(PayrollJournalLine.journal_id == j.id)
                .order_by(PayrollJournalLine.credit, PayrollJournalLine.account)
            )
        )
        views.append(
            JournalView(
                kind=j.kind,
                journal_date=j.journal_date,
                memo=j.memo,
                status=j.status,
                lines=[
                    (
                        r.account,
                        labels.get(r.cost_centre_id) if r.cost_centre_id else None,
                        r.debit,
                        r.credit,
                    )
                    for r in rows
                ],
                debit=sum((r.debit for r in rows), ZERO),
                credit=sum((r.credit for r in rows), ZERO),
            )
        )
    return views
