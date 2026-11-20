"""Payroll runs (ADR 0021): creating and calculating a run for a pay group and month, the
lifecycle with maker-checker at each step, payslip PDFs at approval, and the views."""

import calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.authz.engine import Resource
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.attendance import service as attendance
from app.modules.hr import service as people
from app.modules.payroll import calc, pdf, reliefs, setup, statutory
from app.modules.payroll.files import PAYSLIP
from app.modules.payroll.models import (
    EmployeePay,
    PayComponent,
    PayGroup,
    PayrollRun,
    Payslip,
    PayslipLine,
)
from app.org import service as org

READ = "payroll.run.read"
PREPARE = "payroll.run.prepare"
REVIEW = "payroll.run.review"
APPROVE = "payroll.run.approve"
POST = "payroll.run.post"
PAY = "payroll.run.pay"
PAYSLIP_READ = "payroll.payslip.read"
COUNTED = ("approved", "posted", "paid")  # runs whose PAYE counts for later months
MIN_NOTE = 5
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def _require(db: Session, actor: Actor, permission: str) -> None:
    if not _can(db, actor, permission):
        raise PermissionDenied()


def period_label(year: int, month: int) -> str:
    return f"{MONTHS[month - 1]} {year}"


def _bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _locked(db: Session, run_id: uuid.UUID) -> PayrollRun:
    run = db.scalars(
        select(PayrollRun).where(PayrollRun.id == run_id).with_for_update()
    ).one_or_none()
    if run is None:
        raise NotFound()
    return run


def _audit(
    db: Session, actor: Actor, run: PayrollRun, action: str, before: str, *, note: str | None = None
) -> None:
    record_by(
        db,
        actor,
        f"payroll.run.{action}",
        entity_type="payroll_run",
        entity_id=str(run.id),
        reason=note,
        changes={"status": {"from": before, "to": run.status}},
    )


# ------------------------------------------------------------------ creating


def create_run(
    db: Session, actor: Actor, *, pay_group_id: uuid.UUID | None, year: int, month: int
) -> uuid.UUID:
    _require(db, actor, PREPARE)
    group = db.get(PayGroup, pay_group_id) if pay_group_id else None
    errors = {}
    if group is None or not group.is_active:
        errors["pay_group_id"] = "Choose a pay group."
    if not 1 <= month <= 12 or not 2000 <= year <= 2100:
        errors["period"] = "Choose the month."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert group is not None  # noqa: S101
    if db.scalar(
        select(PayrollRun.id).where(
            PayrollRun.pay_group_id == group.id, PayrollRun.year == year, PayrollRun.month == month
        )
    ):
        raise Conflict(f"There is already a {period_label(year, month)} run for {group.name}.")
    run = PayrollRun(pay_group_id=group.id, year=year, month=month, created_by=actor.user_id)
    db.add(run)
    db.flush()
    record_by(
        db,
        actor,
        "payroll.run.create",
        entity_type="payroll_run",
        entity_id=str(run.id),
        changes={"period": {"from": None, "to": f"{year}-{month:02d} {group.name}"}},
    )
    return run.id


def delete_draft(db: Session, actor: Actor, run_id: uuid.UUID) -> None:
    _require(db, actor, PREPARE)
    run = _locked(db, run_id)
    if run.status not in {"draft", "calculated"}:
        raise Conflict("Only a run that hasn't been reviewed can be deleted.")
    db.delete(run)
    record_by(db, actor, "payroll.run.delete", entity_type="payroll_run", entity_id=str(run_id))


# ------------------------------------------------------------------ calculating


@dataclass(frozen=True, slots=True)
class _Person:
    employee_id: uuid.UUID
    pay: EmployeePay
    days_paid: int


def _people(db: Session, run: PayrollRun) -> list[_Person]:
    """Everyone whose pay record on their last day in the month is in this group."""
    first, last = _bounds(run.year, run.month)
    candidates = list(
        db.scalars(
            select(EmployeePay.employee_id)
            .where(
                EmployeePay.start_date <= last,
                (EmployeePay.end_date.is_(None)) | (EmployeePay.end_date >= first),
            )
            .distinct()
        )
    )
    facts = people.payroll_facts(db, candidates)
    result = []
    for employee_id in candidates:
        f = facts.get(employee_id)
        if f is None:
            continue
        start = max(first, f.hire_date) if f.hire_date else first
        end = min(last, f.last_day) if f.last_day else last
        if start > end:
            continue
        pay = setup.pay_in_force(db, [employee_id], end).get(employee_id)
        if pay is None or pay.pay_group_id != run.pay_group_id:
            continue
        result.append(_Person(employee_id, pay, (end - start).days + 1))
    return result


def _year_to_date(
    db: Session, run: PayrollRun, employee_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    if not employee_ids:
        return {}
    rows = db.execute(
        select(Payslip.employee_id, func.sum(Payslip.taxable), func.sum(Payslip.paye))
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(
            Payslip.employee_id.in_(employee_ids),
            PayrollRun.year == run.year,
            PayrollRun.month < run.month,
            PayrollRun.status.in_(COUNTED),
        )
        .group_by(Payslip.employee_id)
    ).all()
    return {eid: (taxable or Decimal(0), paye or Decimal(0)) for eid, taxable, paye in rows}


def calculate(db: Session, actor: Actor, run_id: uuid.UUID) -> int:
    """(Re)calculate everyone in the run. Returns how many payslips were made."""
    _require(db, actor, PREPARE)
    run = _locked(db, run_id)
    if run.status not in {"draft", "calculated"}:
        raise Conflict("This run has been reviewed; send it back to draft to recalculate.")
    first, last = _bounds(run.year, run.month)
    rules = statutory.in_force(db, last)
    if rules is None:
        raise Conflict(
            "There are no statutory settings for this period. Add them under Payroll → Settings."
        )
    rates = statutory.rates_of(db, rules)
    for old in db.scalars(select(Payslip).where(Payslip.run_id == run.id)):
        db.delete(old)
    db.flush()
    members = _people(db, run)
    ids = [p.employee_id for p in members]
    components = {c.id: c for c in db.scalars(select(PayComponent))}
    amounts = setup.amounts_of(db, [p.pay.id for p in members])
    overtime = attendance.approved_overtime_minutes(db, ids, first, last)
    deductions = setup.deductions_of(db, ids, first)
    rents = reliefs.verified_rent(db, ids, run.year)
    ytd = _year_to_date(db, run, ids)
    days = last.day
    for person in members:
        own = amounts.get(person.pay.id, {})
        on_pay = [components[cid] for cid in own if cid in components]
        calc_components = [
            calc.Component(
                code=c.code,
                name=c.name,
                kind=c.kind,
                calculation=c.calculation,
                percent=c.percent,
                base_code=components[c.base_component_id].code
                if c.base_component_id in components
                else None,
                taxable=c.taxable,
                pensionable=c.pensionable,
            )
            for c in sorted(on_pay, key=lambda c: c.sort_order)
        ]
        taxable_before, paye_before = ytd.get(person.employee_id, (Decimal(0), Decimal(0)))
        result = calc.calculate(
            calc.Inputs(
                month=run.month,
                days_in_month=days,
                days_paid=person.days_paid,
                components=calc_components,
                amounts={components[cid].code: amount for cid, amount in own.items()},
                overtime_minutes=overtime.get(person.employee_id, 0),
                deductions=deductions.get(person.employee_id, []),
                annual_rent=rents.get(person.employee_id),
                taxable_before=taxable_before,
                paye_before=paye_before,
            ),
            rates,
        )
        slip = Payslip(
            run_id=run.id,
            employee_id=person.employee_id,
            days_in_month=days,
            days_paid=person.days_paid,
            gross=result.gross,
            taxable=result.taxable,
            paye=result.paye,
            pension_employee=result.pension_employee,
            pension_employer=result.pension_employer,
            nhf=result.nhf,
            nhis=result.nhis,
            nsitf=result.nsitf,
            other_deductions=result.other_deductions,
            net=result.net,
            rent_relief=result.rent_relief,
            ytd_taxable=result.ytd_taxable,
            ytd_paye=result.ytd_paye,
            warnings="\n".join(result.warnings) or None,
        )
        db.add(slip)
        db.flush()
        for position, line in enumerate(result.lines, start=1):
            db.add(
                PayslipLine(
                    payslip_id=slip.id,
                    position=position,
                    code=line.code,
                    name=line.name,
                    kind=line.kind,
                    amount=line.amount,
                    detail=line.detail,
                )
            )
    before = run.status
    run.status, run.calculated_by, run.calculated_at = "calculated", actor.user_id, clock.now()
    run.statutory_set_id, run.returned_note = rules.id, None
    run.reviewed_by = run.reviewed_at = None
    db.flush()
    _audit(db, actor, run, "calculate", before)
    return len(members)


# ------------------------------------------------------------------ the lifecycle


def _different(actor: Actor, *others: uuid.UUID | None, message: str) -> None:
    if actor.user_id in {o for o in others if o}:
        raise PermissionDenied(message)


def review(db: Session, actor: Actor, run_id: uuid.UUID) -> None:
    _require(db, actor, REVIEW)
    run = _locked(db, run_id)
    if run.status != "calculated":
        raise Conflict("Only a calculated run can be reviewed.")
    _different(
        actor, run.calculated_by, message="You calculated this run, so someone else must review it."
    )
    run.status, run.reviewed_by, run.reviewed_at = "reviewed", actor.user_id, clock.now()
    _audit(db, actor, run, "review", "calculated")


def approve(db: Session, actor: Actor, run_id: uuid.UUID) -> None:
    """Approve a reviewed run and generate everyone's payslip PDF."""
    _require(db, actor, APPROVE)
    run = _locked(db, run_id)
    if run.status != "reviewed":
        raise Conflict("Only a reviewed run can be approved.")
    _different(
        actor, run.reviewed_by, message="You reviewed this run, so someone else must approve it."
    )
    run.status, run.approved_by, run.approved_at = "approved", actor.user_id, clock.now()
    db.flush()
    _payslip_files(db, actor, run)
    _audit(db, actor, run, "approve", "reviewed")


def post(db: Session, actor: Actor, run_id: uuid.UUID) -> None:
    _require(db, actor, POST)
    run = _locked(db, run_id)
    if run.status != "approved":
        raise Conflict("Only an approved run can be posted.")
    _different(
        actor, run.approved_by, message="You approved this run, so someone else must post it."
    )
    _different(actor, run.created_by, message="You created this run, so someone else must post it.")
    run.status, run.posted_by, run.posted_at = "posted", actor.user_id, clock.now()
    _audit(db, actor, run, "post", "approved")


def mark_paid(db: Session, actor: Actor, run_id: uuid.UUID) -> None:
    _require(db, actor, PAY)
    run = _locked(db, run_id)
    if run.status != "posted":
        raise Conflict("Only a posted run can be marked paid.")
    _different(
        actor, run.posted_by, message="You posted this run, so someone else must mark it paid."
    )
    _different(
        actor, run.created_by, message="You created this run, so someone else must mark it paid."
    )
    run.status, run.paid_by, run.paid_at = "paid", actor.user_id, clock.now()
    _audit(db, actor, run, "pay", "posted")


def send_back(db: Session, actor: Actor, run_id: uuid.UUID, *, note: str) -> None:
    """A reviewer or approver returns a calculated or reviewed run to draft."""
    if not (_can(db, actor, REVIEW) or _can(db, actor, APPROVE)):
        raise PermissionDenied()
    run = _locked(db, run_id)
    if run.status not in {"calculated", "reviewed"}:
        raise Conflict("Only a calculated or reviewed run can be sent back.")
    note = note.strip()
    if len(note) < MIN_NOTE:
        raise ValidationFailed(field_errors={"note": "Say what needs changing."})
    before = run.status
    run.status, run.returned_note = "draft", note
    run.reviewed_by = run.reviewed_at = None
    _audit(db, actor, run, "send_back", before, note=note)


# ------------------------------------------------------------------ payslip PDFs


def _payslip_files(db: Session, actor: Actor, run: PayrollRun) -> None:
    organisation = org.get_organization(db)
    group = db.get(PayGroup, run.pay_group_id)
    slips = list(db.scalars(select(Payslip).where(Payslip.run_id == run.id)))
    facts = people.payroll_facts(db, [s.employee_id for s in slips])
    lines: dict[uuid.UUID, list[PayslipLine]] = defaultdict(list)
    for line in db.scalars(
        select(PayslipLine)
        .where(PayslipLine.payslip_id.in_([s.id for s in slips]))
        .order_by(PayslipLine.position)
    ):
        lines[line.payslip_id].append(line)
    for slip in slips:
        f = facts.get(slip.employee_id)
        doc = pdf.PayslipDoc(
            organisation=(organisation.trading_name or organisation.legal_name)
            if organisation
            else "Kxmeleon ERP",
            period=period_label(run.year, run.month),
            pay_group=group.name if group else "",
            name=f.name if f else "",
            number=f.number if f else None,
            pfa=f.pfa if f else None,
            days_paid=slip.days_paid,
            days_in_month=slip.days_in_month,
            earnings=[
                (ln.name, ln.detail, ln.amount) for ln in lines[slip.id] if ln.kind == "earning"
            ],
            deductions=[
                (ln.name, ln.detail, ln.amount) for ln in lines[slip.id] if ln.kind == "deduction"
            ],
            gross=slip.gross,
            net=slip.net,
            employer=[("Pension (employer)", slip.pension_employer), ("NSITF", slip.nsitf)],
            ytd=[("Taxable pay", slip.ytd_taxable), ("PAYE", slip.ytd_paye)],
        )
        number = (f.number if f and f.number else str(slip.employee_id)[:8]).replace("/", "-")
        slip.file_id = files.upload(
            db,
            actor,
            purpose_key=PAYSLIP.key,
            entity_id=str(slip.id),
            filename=f"payslip-{run.year}-{run.month:02d}-{number}.pdf",
            declared_mime="application/pdf",
            data=pdf.render(doc),
        )


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class PayslipView:
    id: uuid.UUID
    employee_id: uuid.UUID
    name: str
    number: str | None
    days_paid: int
    days_in_month: int
    gross: Decimal
    taxable: Decimal
    paye: Decimal
    pension_employee: Decimal
    pension_employer: Decimal
    nhf: Decimal
    nhis: Decimal
    nsitf: Decimal
    other_deductions: Decimal
    net: Decimal
    rent_relief: Decimal
    ytd_taxable: Decimal
    ytd_paye: Decimal
    warnings: list[str]
    file_id: uuid.UUID | None
    lines: list[PayslipLine] = field(default_factory=list)
    period: str = ""
    paid_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RunView:
    id: uuid.UUID
    year: int
    month: int
    period: str
    pay_group: str
    status: str
    people: int
    gross: Decimal
    net: Decimal
    paye: Decimal
    pension_employee: Decimal
    pension_employer: Decimal
    nhf: Decimal
    nsitf: Decimal
    employer_cost: Decimal
    steps: list[tuple[str, str | None, datetime | None]]  # (step, by, at)
    returned_note: str | None
    created_by: uuid.UUID | None
    payslips: list[PayslipView] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    can: dict[str, bool] = field(default_factory=dict)
    blocked: dict[str, str] = field(default_factory=dict)  # why a step is not for this actor


def _totals(
    db: Session, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]]:
    if not run_ids:
        return {}
    rows = db.execute(
        select(
            Payslip.run_id,
            func.count(),
            func.sum(Payslip.gross),
            func.sum(Payslip.net),
            func.sum(Payslip.paye),
            func.sum(Payslip.pension_employee),
            func.sum(Payslip.pension_employer),
            func.sum(Payslip.nhf),
            func.sum(Payslip.nsitf),
        )
        .where(Payslip.run_id.in_(run_ids))
        .group_by(Payslip.run_id)
    ).all()
    return {r[0]: tuple(r[1:]) for r in rows}  # type: ignore[misc]


def _who(db: Session, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    return (
        dict(db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all())
        if wanted
        else {}
    )


def _permissions(
    db: Session, actor: Actor, run: PayrollRun
) -> tuple[dict[str, bool], dict[str, str]]:
    me = actor.user_id
    can = {
        "calculate": run.status in {"draft", "calculated"} and _can(db, actor, PREPARE),
        "delete": run.status in {"draft", "calculated"} and _can(db, actor, PREPARE),
        "review": run.status == "calculated" and _can(db, actor, REVIEW),
        "approve": run.status == "reviewed" and _can(db, actor, APPROVE),
        "post": run.status == "approved" and _can(db, actor, POST),
        "pay": run.status == "posted" and _can(db, actor, PAY),
        "send_back": run.status in {"calculated", "reviewed"}
        and (_can(db, actor, REVIEW) or _can(db, actor, APPROVE)),
    }
    blocked = {}
    if can["review"] and me == run.calculated_by:
        can["review"], blocked["review"] = (
            False,
            "You calculated this run, so someone else must review it.",
        )
    if can["approve"] and me == run.reviewed_by:
        can["approve"], blocked["approve"] = (
            False,
            "You reviewed this run, so someone else must approve it.",
        )
    if can["post"] and me in {run.approved_by, run.created_by}:
        can["post"], blocked["post"] = (
            False,
            "You approved or created this run, so someone else must post it.",
        )
    if can["pay"] and me in {run.posted_by, run.created_by}:
        can["pay"], blocked["pay"] = (
            False,
            "You posted or created this run, so someone else must mark it paid.",
        )
    return can, blocked


def _run_views(db: Session, actor: Actor, runs: list[PayrollRun]) -> list[RunView]:
    groups = {g.id: g.name for g in db.scalars(select(PayGroup))}
    totals = _totals(db, [r.id for r in runs])
    who = _who(
        db,
        {
            u
            for r in runs
            for u in (
                r.created_by,
                r.calculated_by,
                r.reviewed_by,
                r.approved_by,
                r.posted_by,
                r.paid_by,
            )
        },
    )
    zero = Decimal(0)
    views = []
    for r in runs:
        count, gross, net, paye, pen_ee, pen_er, nhf, nsitf = totals.get(
            r.id, (0, zero, zero, zero, zero, zero, zero, zero)
        )
        can, blocked = _permissions(db, actor, r)
        views.append(
            RunView(
                id=r.id,
                year=r.year,
                month=r.month,
                period=period_label(r.year, r.month),
                pay_group=groups.get(r.pay_group_id, ""),
                status=r.status,
                people=count,
                gross=gross or zero,
                net=net or zero,
                paye=paye or zero,
                pension_employee=pen_ee or zero,
                pension_employer=pen_er or zero,
                nhf=nhf or zero,
                nsitf=nsitf or zero,
                employer_cost=(gross or zero) + (pen_er or zero) + (nsitf or zero),
                steps=[
                    ("Created", who.get(r.created_by) if r.created_by else None, r.created_at),
                    (
                        "Calculated",
                        who.get(r.calculated_by) if r.calculated_by else None,
                        r.calculated_at,
                    ),
                    ("Reviewed", who.get(r.reviewed_by) if r.reviewed_by else None, r.reviewed_at),
                    ("Approved", who.get(r.approved_by) if r.approved_by else None, r.approved_at),
                    ("Posted", who.get(r.posted_by) if r.posted_by else None, r.posted_at),
                    ("Paid", who.get(r.paid_by) if r.paid_by else None, r.paid_at),
                ],
                returned_note=r.returned_note,
                created_by=r.created_by,
                can=can,
                blocked=blocked,
            )
        )
    return views


def list_runs(db: Session, actor: Actor) -> list[RunView]:
    _require(db, actor, READ)
    runs = list(
        db.scalars(
            select(PayrollRun).order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).limit(120)
        )
    )
    return _run_views(db, actor, runs)


def _slip_views(db: Session, slips: list[Payslip]) -> list[PayslipView]:
    facts = people.payroll_facts(db, [s.employee_id for s in slips])
    lines: dict[uuid.UUID, list[PayslipLine]] = defaultdict(list)
    for line in db.scalars(
        select(PayslipLine)
        .where(PayslipLine.payslip_id.in_([s.id for s in slips]))
        .order_by(PayslipLine.position)
    ):
        lines[line.payslip_id].append(line)
    runs = {
        r.id: r
        for r in db.scalars(select(PayrollRun).where(PayrollRun.id.in_({s.run_id for s in slips})))
    }
    return [
        PayslipView(
            id=s.id,
            employee_id=s.employee_id,
            name=facts[s.employee_id].name if s.employee_id in facts else "",
            number=facts[s.employee_id].number if s.employee_id in facts else None,
            days_paid=s.days_paid,
            days_in_month=s.days_in_month,
            gross=s.gross,
            taxable=s.taxable,
            paye=s.paye,
            pension_employee=s.pension_employee,
            pension_employer=s.pension_employer,
            nhf=s.nhf,
            nhis=s.nhis,
            nsitf=s.nsitf,
            other_deductions=s.other_deductions,
            net=s.net,
            rent_relief=s.rent_relief,
            ytd_taxable=s.ytd_taxable,
            ytd_paye=s.ytd_paye,
            warnings=s.warnings.splitlines() if s.warnings else [],
            file_id=s.file_id,
            lines=lines[s.id],
            period=period_label(runs[s.run_id].year, runs[s.run_id].month),
            paid_at=runs[s.run_id].paid_at,
        )
        for s in slips
    ]


def get_run(db: Session, actor: Actor, run_id: uuid.UUID) -> RunView:
    _require(db, actor, READ)
    run = db.get(PayrollRun, run_id)
    if run is None:
        raise NotFound()
    view = _run_views(db, actor, [run])[0]
    slips = list(db.scalars(select(Payslip).where(Payslip.run_id == run.id)))
    slip_views = sorted(_slip_views(db, slips), key=lambda s: s.name)
    view.payslips.extend(slip_views)
    for s in slip_views:
        view.warnings.extend(f"{s.name}: {w}" for w in s.warnings)
    earlier = db.scalar(
        select(func.count())
        .select_from(PayrollRun)
        .where(
            PayrollRun.pay_group_id == run.pay_group_id,
            PayrollRun.year == run.year,
            PayrollRun.month < run.month,
            PayrollRun.status.not_in(COUNTED),
        )
    )
    if earlier and run.status in {"draft", "calculated"}:
        view.warnings.insert(
            0,
            "An earlier run this year isn't approved yet, so its PAYE isn't counted in "
            "year-to-date figures. Approve it first, then recalculate.",
        )
    return view


def my_payslips(db: Session, actor: Actor) -> list[PayslipView]:
    """The actor's payslips from paid runs, newest first."""
    employee_id = people.employee_of_user(db, actor.user_id)
    if employee_id is None or not engine.check(
        db, actor.user_id, PAYSLIP_READ, Resource(owner_employee_id=employee_id)
    ):
        return []
    slips = list(
        db.scalars(
            select(Payslip)
            .join(PayrollRun, PayrollRun.id == Payslip.run_id)
            .where(Payslip.employee_id == employee_id, PayrollRun.status == "paid")
            .order_by(PayrollRun.year.desc(), PayrollRun.month.desc())
        )
    )
    return _slip_views(db, slips)
