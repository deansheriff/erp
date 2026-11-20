"""Year-end tax certificates (ADR 0022): pay, pension, NHF and NHIS, taxable pay and PAYE
by month from approved runs. Payroll can download them at any time; employees download
their own once the December run that pays them is paid (or, if they left before
December, once the year is over and their runs are paid)."""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.authz.engine import Resource
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.payroll import pdf
from app.modules.payroll.models import COUNTED, PayrollRun, Payslip
from app.modules.payroll.service import MONTHS
from app.org import service as org

READ = "payroll.certificate.read"
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class CertificateRow:
    employee_id: uuid.UUID
    name: str
    number: str | None
    state: str | None
    months: int
    gross: Decimal
    pension: Decimal
    nhf: Decimal
    rent_relief: Decimal
    taxable: Decimal
    paye: Decimal


@dataclass(frozen=True, slots=True)
class YearView:
    year: int
    years: list[int]
    december_paid: bool  # every December run of the year is paid
    rows: list[CertificateRow]


def _slips(
    db: Session, year: int, employee_ids: list[uuid.UUID] | None = None
) -> list[tuple[Payslip, PayrollRun]]:
    query = (
        select(Payslip, PayrollRun)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(PayrollRun.year == year, PayrollRun.status.in_(COUNTED))
        .order_by(PayrollRun.month)
    )
    if employee_ids is not None:
        query = query.where(Payslip.employee_id.in_(employee_ids))
    return [(s, r) for s, r in db.execute(query).all()]


def years(db: Session) -> list[int]:
    return list(
        db.scalars(
            select(PayrollRun.year)
            .where(PayrollRun.status.in_(COUNTED))
            .distinct()
            .order_by(PayrollRun.year.desc())
        )
    )


def _december_paid(db: Session, year: int) -> bool:
    statuses = list(
        db.scalars(select(PayrollRun.status).where(PayrollRun.year == year, PayrollRun.month == 12))
    )
    return bool(statuses) and all(s == "paid" for s in statuses)


def year_view(db: Session, actor: Actor, year: int | None) -> YearView:
    if not engine.check(db, actor.user_id, READ):
        raise PermissionDenied()
    known = years(db)
    chosen = year if year in known else (known[0] if known else clock.today().year)
    by_person: dict[uuid.UUID, list[Payslip]] = defaultdict(list)
    for slip, _ in _slips(db, chosen):
        if engine.check(db, actor.user_id, READ, Resource(owner_employee_id=slip.employee_id)):
            by_person[slip.employee_id].append(slip)
    facts = people.payroll_facts(db, list(by_person))
    rows = []
    for employee_id, slips in by_person.items():
        f = facts.get(employee_id)
        rows.append(
            CertificateRow(
                employee_id=employee_id,
                name=f.name if f else "",
                number=f.number if f else None,
                state=f.state if f else None,
                months=len(slips),
                gross=sum((s.gross for s in slips), ZERO),
                pension=sum((s.pension_employee for s in slips), ZERO),
                nhf=sum((s.nhf + s.nhis for s in slips), ZERO),
                rent_relief=sum((s.rent_relief for s in slips), ZERO),
                taxable=sum((s.taxable for s in slips), ZERO),
                paye=sum((s.paye for s in slips), ZERO),
            )
        )
    return YearView(
        year=chosen,
        years=known or [chosen],
        december_paid=_december_paid(db, chosen),
        rows=sorted(rows, key=lambda r: r.name),
    )


def _available_to_self(slips: list[tuple[Payslip, PayrollRun]], year: int) -> bool:
    if not slips or any(run.status != "paid" for _, run in slips):
        return False
    if any(run.month == 12 for _, run in slips):
        return True
    return clock.today().year > year  # left before December: once the year is over


def my_years(db: Session, actor: Actor) -> list[int]:
    """Tax years whose certificate the signed-in employee can download."""
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None or not engine.check(
        db, actor.user_id, READ, Resource(owner_employee_id=employee_id)
    ):
        return []
    by_year: dict[int, list[tuple[Payslip, PayrollRun]]] = defaultdict(list)
    for slip, run in db.execute(
        select(Payslip, PayrollRun)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(Payslip.employee_id == employee_id, PayrollRun.status.in_(COUNTED))
    ).all():
        by_year[run.year].append((slip, run))
    return sorted((y for y, s in by_year.items() if _available_to_self(s, y)), reverse=True)


def certificate(db: Session, actor: Actor, employee_id: uuid.UUID, year: int) -> tuple[str, bytes]:
    """The certificate PDF: (filename, bytes). Employees get their own only once it is
    available; out of scope is 404."""
    if not engine.check(db, actor.user_id, READ, Resource(owner_employee_id=employee_id)):
        raise NotFound()
    slips = _slips(db, year, [employee_id])
    if not slips:
        raise NotFound()
    own = people.employee_of_user(db, actor.user_id) == employee_id
    wide = engine.check(db, actor.user_id, READ, Resource(owner_employee_id=None))
    if own and not wide and not _available_to_self(slips, year):
        raise Conflict("Your certificate is ready once December's payroll is paid.")
    facts = people.payroll_facts(db, [employee_id]).get(employee_id)
    details = people.remittance_details(db, [employee_id]).get(employee_id)
    organisation = org.get_organization(db)
    doc = pdf.CertificateDoc(
        organisation=(organisation.trading_name or organisation.legal_name) if organisation else "",
        year=year,
        name=facts.name if facts else "",
        number=facts.number if facts else None,
        tin=details.tin if details else None,
        state=facts.state if facts else None,
        months=[
            (
                MONTHS[run.month - 1],
                s.gross,
                s.pension_employee,
                s.nhf + s.nhis,
                s.taxable,
                s.paye,
            )
            for s, run in slips
        ],
        rent_relief=sum((s.rent_relief for s, _ in slips), ZERO),
    )
    record_by(
        db,
        actor,
        "payroll.certificate.download",
        entity_type="employee",
        entity_id=str(employee_id),
        changes={"year": {"from": None, "to": year}},
    )
    return (
        f"tax-certificate-{year}-{facts.number if facts and facts.number else 'employee'}.pdf",
        pdf.render_certificate(doc),
    )
