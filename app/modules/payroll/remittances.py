"""Remittances from an approved run (ADR 0022): the bank payment file and the PAYE (per
state of residence), pension (per PFA) and NHF schedules, as CSV. They carry decrypted
identifiers, so downloads need `payroll.remittance.export` and are audited."""

import csv
import io
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core.errors import Conflict, NotFound, PermissionDenied
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.payroll import ledger
from app.modules.payroll.models import (
    COUNTED,
    PayGroup,
    PayrollRun,
    Payslip,
    PayslipLine,
    StatutorySet,
)
from app.modules.payroll.service import period_label

READ = "payroll.run.read"
EXPORT = "payroll.remittance.export"
NOT_SET = "Not set"
NUBAN = re.compile(r"\d{10}")
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Group:
    label: str  # a state or a PFA
    people: int
    amounts: tuple[Decimal, ...]  # PAYE: (taxable, PAYE); pension: (employee, employer)


@dataclass(frozen=True, slots=True)
class Remittances:
    run_id: uuid.UUID
    period: str
    pay_group: str
    status: str
    people: int
    bank_people: int
    bank_total: Decimal
    left_out: list[tuple[str, str]]  # (name, why)
    paye: list[Group]
    pension: list[Group]
    nhf_people: int
    nhf_total: Decimal
    nsitf: Decimal
    itf: Decimal  # this month's share; ITF is an annual return
    journals: list[ledger.JournalView] = field(default_factory=list)
    can_export: bool = False

    @property
    def paye_total(self) -> Decimal:
        return sum((g.amounts[1] for g in self.paye), ZERO)

    @property
    def pension_total(self) -> Decimal:
        return sum((g.amounts[0] + g.amounts[1] for g in self.pension), ZERO)


@dataclass(frozen=True, slots=True)
class _Person:
    slip: Payslip
    name: str
    number: str | None
    state: str | None
    pfa: str | None
    basic: Decimal


def _cell(value: object) -> str:
    """Spreadsheet-safe text: values that would start a formula get a leading quote."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _csv(header: list[str], rows: list[list[object]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows([[_cell(v) for v in row] for row in rows])
    return out.getvalue()


def ready_runs(db: Session) -> list[tuple[uuid.UUID, str]]:
    """Runs remittances can be taken from, newest first: (id, label)."""
    groups = dict(db.execute(select(PayGroup.id, PayGroup.name)).all())
    return [
        (r.id, f"{period_label(r.year, r.month)} · {groups.get(r.pay_group_id, '')}")
        for r in db.scalars(
            select(PayrollRun)
            .where(PayrollRun.status.in_(COUNTED))
            .order_by(PayrollRun.year.desc(), PayrollRun.month.desc())
        )
    ]


def _run(db: Session, actor: Actor, run_id: uuid.UUID, permission: str) -> PayrollRun:
    if not engine.check(db, actor.user_id, permission):
        raise PermissionDenied()
    run = db.get(PayrollRun, run_id)
    if run is None:
        raise NotFound()
    if run.status not in COUNTED:
        raise Conflict("Remittances come from a run once it is approved.")
    return run


def _people(db: Session, run: PayrollRun) -> list[_Person]:
    slips = list(db.scalars(select(Payslip).where(Payslip.run_id == run.id)))
    facts = people.payroll_facts(db, [s.employee_id for s in slips])
    basic = dict(
        db.execute(
            select(PayslipLine.payslip_id, PayslipLine.amount).where(
                PayslipLine.payslip_id.in_([s.id for s in slips]), PayslipLine.code == "basic"
            )
        ).all()
    )
    rows = []
    for s in slips:
        f = facts.get(s.employee_id)
        rows.append(
            _Person(
                slip=s,
                name=f.name if f else "",
                number=f.number if f else None,
                state=f.state if f else None,
                pfa=f.pfa if f else None,
                basic=basic.get(s.id, ZERO),
            )
        )
    return sorted(rows, key=lambda p: p.name)


def _bank_rows(
    db: Session, rows: list[_Person]
) -> tuple[list[tuple[_Person, str, str, str, str]], list[tuple[str, str]]]:
    """(person, account name, account number, bank code, bank) for those who can be paid,
    and (name, why) for those left out."""
    details = people.remittance_details(db, [p.slip.employee_id for p in rows])
    match = ledger.bank_matcher(db)
    paid, left_out = [], []
    for p in rows:
        if p.slip.net <= 0:
            left_out.append((p.name, "No net pay"))
            continue
        d = details.get(p.slip.employee_id)
        account = (d.account_number or "").strip() if d else ""
        bank = match(d.bank_name if d else None)
        if not NUBAN.fullmatch(account):
            left_out.append((p.name, "No 10-digit account number"))
        elif bank is None:
            why = f"Bank not on the list: {d.bank_name}" if d and d.bank_name else "No bank"
            left_out.append((p.name, why))
        else:
            paid.append(
                (p, (d.account_name if d else None) or p.name, account, bank.code, bank.name)
            )
    return paid, left_out


def summary(db: Session, actor: Actor, run_id: uuid.UUID) -> Remittances:
    run = _run(db, actor, run_id, READ)
    rows = _people(db, run)
    group = db.get(PayGroup, run.pay_group_id)
    rules = db.get(StatutorySet, run.statutory_set_id) if run.statutory_set_id else None
    paid, left_out = _bank_rows(db, rows)
    by_state: dict[str, list[_Person]] = defaultdict(list)
    by_pfa: dict[str, list[_Person]] = defaultdict(list)
    for p in rows:
        if p.slip.paye:
            by_state[p.state or NOT_SET].append(p)
        if p.slip.pension_employee or p.slip.pension_employer:
            by_pfa[p.pfa or NOT_SET].append(p)
    nhf = [p for p in rows if p.slip.nhf]
    gross = sum((p.slip.gross for p in rows), ZERO)
    return Remittances(
        run_id=run.id,
        period=period_label(run.year, run.month),
        pay_group=group.name if group else "",
        status=run.status,
        people=len(rows),
        bank_people=len(paid),
        bank_total=sum((p.slip.net for p, *_ in paid), ZERO),
        left_out=left_out,
        paye=[
            Group(
                label,
                len(ps),
                (sum((p.slip.taxable for p in ps), ZERO), sum((p.slip.paye for p in ps), ZERO)),
            )
            for label, ps in sorted(by_state.items(), key=lambda kv: (kv[0] == NOT_SET, kv[0]))
        ],
        pension=[
            Group(
                label,
                len(ps),
                (
                    sum((p.slip.pension_employee for p in ps), ZERO),
                    sum((p.slip.pension_employer for p in ps), ZERO),
                ),
            )
            for label, ps in sorted(by_pfa.items(), key=lambda kv: (kv[0] == NOT_SET, kv[0]))
        ],
        nhf_people=len(nhf),
        nhf_total=sum((p.slip.nhf for p in nhf), ZERO),
        nsitf=sum((p.slip.nsitf for p in rows), ZERO),
        itf=(gross * rules.itf_rate).quantize(Decimal("0.01")) if rules else ZERO,
        journals=ledger.journals_of_run(db, run.id),
        can_export=bool(engine.check(db, actor.user_id, EXPORT)),
    )


# ------------------------------------------------------------------ downloads


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "all"


def _audit(db: Session, actor: Actor, run: PayrollRun, what: str, people_count: int) -> None:
    record_by(
        db,
        actor,
        "payroll.remittance.download",
        entity_type="payroll_run",
        entity_id=str(run.id),
        changes={"file": {"from": None, "to": what}, "people": {"from": None, "to": people_count}},
    )


def bank_file(db: Session, actor: Actor, run_id: uuid.UUID) -> tuple[str, str]:
    run = _run(db, actor, run_id, EXPORT)
    paid, _ = _bank_rows(db, _people(db, run))
    narration = f"Salary {period_label(run.year, run.month)}"
    text = _csv(
        ["Account name", "Account number", "Bank code", "Bank", "Amount", "Narration"],
        [
            [name, account, code, bank, f"{p.slip.net:.2f}", narration]
            for p, name, account, code, bank in paid
        ],
    )
    _audit(db, actor, run, "bank", len(paid))
    return f"bank-{run.year}-{run.month:02d}.csv", text


def paye_schedule(db: Session, actor: Actor, run_id: uuid.UUID, state: str = "") -> tuple[str, str]:
    run = _run(db, actor, run_id, EXPORT)
    rows = [p for p in _people(db, run) if p.slip.paye]
    if state:
        rows = [p for p in rows if (p.state or NOT_SET) == state]
    details = people.remittance_details(db, [p.slip.employee_id for p in rows])
    text = _csv(
        ["State", "Employee number", "Name", "TIN", "Gross pay", "Taxable pay", "PAYE"],
        [
            [
                p.state or NOT_SET,
                p.number,
                p.name,
                details[p.slip.employee_id].tin if p.slip.employee_id in details else "",
                f"{p.slip.gross:.2f}",
                f"{p.slip.taxable:.2f}",
                f"{p.slip.paye:.2f}",
            ]
            for p in sorted(rows, key=lambda p: (p.state or NOT_SET, p.name))
        ],
    )
    _audit(db, actor, run, f"paye:{state or 'all'}", len(rows))
    return f"paye-{_slug(state)}-{run.year}-{run.month:02d}.csv", text


def pension_schedule(
    db: Session, actor: Actor, run_id: uuid.UUID, pfa: str = ""
) -> tuple[str, str]:
    run = _run(db, actor, run_id, EXPORT)
    rows = [p for p in _people(db, run) if p.slip.pension_employee or p.slip.pension_employer]
    if pfa:
        rows = [p for p in rows if (p.pfa or NOT_SET) == pfa]
    details = people.remittance_details(db, [p.slip.employee_id for p in rows])
    text = _csv(
        ["PFA", "Employee number", "Name", "RSA PIN", "Employee", "Employer", "Total"],
        [
            [
                p.pfa or NOT_SET,
                p.number,
                p.name,
                details[p.slip.employee_id].rsa_pin if p.slip.employee_id in details else "",
                f"{p.slip.pension_employee:.2f}",
                f"{p.slip.pension_employer:.2f}",
                f"{p.slip.pension_employee + p.slip.pension_employer:.2f}",
            ]
            for p in sorted(rows, key=lambda p: (p.pfa or NOT_SET, p.name))
        ],
    )
    _audit(db, actor, run, f"pension:{pfa or 'all'}", len(rows))
    return f"pension-{_slug(pfa)}-{run.year}-{run.month:02d}.csv", text


def nhf_schedule(db: Session, actor: Actor, run_id: uuid.UUID) -> tuple[str, str]:
    run = _run(db, actor, run_id, EXPORT)
    rows = [p for p in _people(db, run) if p.slip.nhf]
    details = people.remittance_details(db, [p.slip.employee_id for p in rows])
    text = _csv(
        ["Employee number", "Name", "NHF number", "Basic pay", "NHF"],
        [
            [
                p.number,
                p.name,
                details[p.slip.employee_id].nhf_number if p.slip.employee_id in details else "",
                f"{p.basic:.2f}",
                f"{p.slip.nhf:.2f}",
            ]
            for p in rows
        ],
    )
    _audit(db, actor, run, "nhf", len(rows))
    return f"nhf-{run.year}-{run.month:02d}.csv", text
