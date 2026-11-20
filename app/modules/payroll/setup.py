"""Pay set-up (ADR 0021): pay groups, components, salary structures, people's pay records
and recurring deductions. Amounts are `compensation` class: the audit log records that
they changed, never the values."""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import catalogue, engine, sod
from app.authz.engine import Resource
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.money import parse_money
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.payroll.models import (
    EmployeeDeduction,
    EmployeePay,
    EmployeePayAmount,
    PayComponent,
    PayGroup,
    SalaryStructure,
    StructureComponent,
)

STRUCTURES = "payroll.structure.manage"
PAY_MANAGE = "payroll.pay.manage"
PAY_READ = "payroll.pay.read"
CODE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")
MAX_CODE = 30
MAX_PERCENT = Decimal(1000)


def _require(
    db: Session, actor: Actor, permission: str, employee_id: uuid.UUID | None = None
) -> None:
    resource = Resource(owner_employee_id=employee_id) if employee_id else None
    if not engine.check(db, actor.user_id, permission, resource):
        raise PermissionDenied()


def _name(value: str, field: str = "name") -> str:
    name = " ".join(value.split())
    if not name:
        raise ValidationFailed(field_errors={field: "Give it a name."})
    return name[:80]


# ------------------------------------------------------------------ pay groups


def list_groups(db: Session, *, active_only: bool = False) -> list[PayGroup]:
    stmt = select(PayGroup).order_by(PayGroup.name)
    return list(db.scalars(stmt.where(PayGroup.is_active) if active_only else stmt))


def save_group(
    db: Session, actor: Actor, group_id: uuid.UUID | None, *, name: str, is_active: bool
) -> uuid.UUID:
    _require(db, actor, STRUCTURES)
    name = _name(name, "group_name")
    clash = db.scalar(
        select(PayGroup.id).where(
            func.lower(PayGroup.name) == name.lower(),
            *([PayGroup.id != group_id] if group_id else []),
        )
    )
    if clash:
        raise ValidationFailed(field_errors={"group_name": "Another pay group has this name."})
    row = db.get(PayGroup, group_id) if group_id else PayGroup()
    if row is None:
        raise NotFound()
    before = {"name": row.name, "active": row.is_active} if group_id else {}
    row.name, row.is_active = name, is_active if group_id else True
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "payroll.group.save",
        entity_type="pay_group",
        entity_id=str(row.id),
        changes=diff_changes(before, {"name": row.name, "active": row.is_active}),
    )
    return row.id


# ------------------------------------------------------------------ components


def list_components(db: Session, *, active_only: bool = False) -> list[PayComponent]:
    stmt = select(PayComponent).order_by(
        PayComponent.kind.desc(), PayComponent.sort_order, PayComponent.name
    )
    return list(db.scalars(stmt.where(PayComponent.is_active) if active_only else stmt))


def _component_values(c: PayComponent) -> dict[str, object]:
    return {
        "code": c.code,
        "name": c.name,
        "kind": c.kind,
        "calculation": c.calculation,
        "percent": str(c.percent) if c.percent is not None else None,
        "base": str(c.base_component_id) if c.base_component_id else None,
        "taxable": c.taxable,
        "pensionable": c.pensionable,
        "active": c.is_active,
    }


@dataclass(frozen=True, slots=True)
class ComponentInput:
    code: str
    name: str
    kind: str
    calculation: str
    percent: str
    base_component_id: uuid.UUID | None
    taxable: bool
    pensionable: bool
    is_active: bool


def _check_component(db: Session, data: ComponentInput) -> tuple[str, Decimal | None]:
    """The cleaned code and, for a percentage component, its rate (10% gives 0.1)."""
    errors: dict[str, str] = {}
    code = data.code.strip().lower()
    if not code or not set(code) <= CODE_CHARS or len(code) > MAX_CODE:
        errors["code"] = "Use lower-case letters, digits and underscores."
    if data.kind not in {"earning", "deduction"}:
        errors["kind"] = "Choose earning or deduction."
    if data.calculation not in {"fixed", "percent"}:
        errors["calculation"] = "Choose fixed or percentage."
    rate: Decimal | None = None
    if data.calculation == "percent":
        if data.kind != "earning":
            errors["calculation"] = "Only earnings can be a percentage."
        base = db.get(PayComponent, data.base_component_id) if data.base_component_id else None
        if base is None or base.kind != "earning" or base.calculation != "fixed":
            errors["base_component_id"] = "Choose a fixed earning to take the percentage of."
        value = parse_money(data.percent, "percent", blank=True)
        if value is None or not Decimal(0) < value <= MAX_PERCENT:
            errors["percent"] = "Enter a percentage above zero."
        else:
            rate = value / 100
    if data.pensionable and data.kind != "earning":
        errors["pensionable"] = "Only earnings are pensionable."
    if errors:
        raise ValidationFailed(field_errors=errors)
    return code, rate


def save_component(
    db: Session, actor: Actor, component_id: uuid.UUID | None, data: ComponentInput
) -> uuid.UUID:
    """A percentage component takes its percentage of one fixed earning (e.g. 10% of
    Basic). Built-in components keep their code and kind."""
    _require(db, actor, STRUCTURES)
    code, rate = _check_component(db, data)
    name = _name(data.name)
    for field_name, column, value in (
        ("code", PayComponent.code, code),
        ("name", PayComponent.name, name),
    ):
        clash = db.scalar(
            select(PayComponent.id).where(
                func.lower(column) == value.lower(),
                *([PayComponent.id != component_id] if component_id else []),
            )
        )
        if clash:
            raise ValidationFailed(
                field_errors={field_name: f"Another component has this {field_name}."}
            )
    if component_id is None:
        row = PayComponent(
            sort_order=(db.scalar(select(func.max(PayComponent.sort_order))) or 0) + 1
        )
        before: dict[str, object] = {}
    else:
        found = db.get(PayComponent, component_id)
        if found is None:
            raise NotFound()
        row = found
        before = _component_values(row)
        if row.system and (code != row.code or data.kind != row.kind):
            raise Conflict("A built-in component keeps its code and kind.")
    row.code, row.name, row.kind, row.calculation = code, name, data.kind, data.calculation
    row.percent = rate
    row.base_component_id = data.base_component_id if data.calculation == "percent" else None
    row.taxable, row.pensionable = data.taxable, data.pensionable
    row.is_active = data.is_active or row.system or component_id is None
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "payroll.component.save",
        entity_type="pay_component",
        entity_id=str(row.id),
        changes=diff_changes(before, _component_values(row)),
    )
    return row.id


# ------------------------------------------------------------------ salary structures


@dataclass(frozen=True, slots=True)
class StructureView:
    id: uuid.UUID
    name: str
    is_active: bool
    lines: list[tuple[uuid.UUID, str, Decimal | None]]  # (component id, name, default amount)


def list_structures(db: Session) -> list[StructureView]:
    components = {c.id: c for c in db.scalars(select(PayComponent))}
    lines: dict[uuid.UUID, list[tuple[uuid.UUID, str, Decimal | None]]] = {}
    for sc in db.scalars(select(StructureComponent)):
        lines.setdefault(sc.structure_id, []).append(
            (sc.component_id, components[sc.component_id].name, sc.default_amount)
        )
    return [
        StructureView(
            s.id,
            s.name,
            s.is_active,
            sorted(lines.get(s.id, []), key=lambda line: components[line[0]].sort_order),
        )
        for s in db.scalars(select(SalaryStructure).order_by(SalaryStructure.name))
    ]


def save_structure(
    db: Session,
    actor: Actor,
    structure_id: uuid.UUID | None,
    *,
    name: str,
    is_active: bool,
    lines: list[tuple[str, str]],  # (component id, default amount)
) -> uuid.UUID:
    _require(db, actor, STRUCTURES)
    name = _name(name, "structure_name")
    clash = db.scalar(
        select(SalaryStructure.id).where(
            func.lower(SalaryStructure.name) == name.lower(),
            *([SalaryStructure.id != structure_id] if structure_id else []),
        )
    )
    if clash:
        raise ValidationFailed(field_errors={"structure_name": "Another structure has this name."})
    components = {
        str(c.id): c for c in db.scalars(select(PayComponent).where(PayComponent.is_active))
    }
    chosen: dict[uuid.UUID, Decimal | None] = {}
    for raw_id, amount in lines:
        component = components.get(raw_id)
        if component is None:
            continue
        default = (
            parse_money(amount, f"default_{raw_id}", blank=True)
            if component.calculation == "fixed"
            else None
        )
        chosen[component.id] = default
    if not any(components[str(cid)].code == "basic" for cid in chosen):
        raise ValidationFailed(field_errors={"structure_name": "A structure needs Basic salary."})
    row = db.get(SalaryStructure, structure_id) if structure_id else SalaryStructure()
    if row is None:
        raise NotFound()
    row.name, row.is_active = name, is_active if structure_id else True
    db.add(row)
    db.flush()
    for existing in db.scalars(
        select(StructureComponent).where(StructureComponent.structure_id == row.id)
    ):
        db.delete(existing)
    db.flush()
    for component_id, default in chosen.items():
        db.add(
            StructureComponent(
                structure_id=row.id, component_id=component_id, default_amount=default
            )
        )
    record_by(
        db,
        actor,
        "payroll.structure.save",
        entity_type="salary_structure",
        entity_id=str(row.id),
        changes={"components": {"from": None, "to": len(chosen)}},
    )
    return row.id


# ------------------------------------------------------------------ people's pay


def pay_in_force(
    db: Session, employee_ids: list[uuid.UUID], on: date
) -> dict[uuid.UUID, EmployeePay]:
    """Each person's pay record in force on a day."""
    if not employee_ids:
        return {}
    found: dict[uuid.UUID, EmployeePay] = {}
    for row in db.scalars(
        select(EmployeePay)
        .where(
            EmployeePay.employee_id.in_(employee_ids),
            EmployeePay.start_date <= on,
            or_(EmployeePay.end_date.is_(None), EmployeePay.end_date >= on),
        )
        .order_by(EmployeePay.start_date.desc())
    ):
        found.setdefault(row.employee_id, row)
    return found


def amounts_of(db: Session, pay_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[uuid.UUID, Decimal]]:
    result: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {}
    for a in db.scalars(select(EmployeePayAmount).where(EmployeePayAmount.pay_id.in_(pay_ids))):
        result.setdefault(a.pay_id, {})[a.component_id] = a.amount
    return result


def monthly_gross(
    components: dict[uuid.UUID, PayComponent], amounts: dict[uuid.UUID, Decimal]
) -> Decimal:
    total = Decimal(0)
    for cid, amount in amounts.items():
        c = components.get(cid)
        if c and c.kind == "earning" and c.calculation == "fixed":
            total += amount
    for c in components.values():
        if (
            c.kind == "earning"
            and c.calculation == "percent"
            and c.id in amounts
            and c.base_component_id in amounts
        ):
            total += amounts[c.base_component_id] * (c.percent or 0)
    return total


def _check_not_self(db: Session, actor: Actor, employee_id: uuid.UUID) -> None:
    facts = people.status_and_account(db, employee_id)
    held = frozenset(engine.load_grants(db, actor.user_id).by_permission)
    problems = sod.violations(
        catalogue.sod_rules(),
        PAY_MANAGE,
        actor.user_id,
        {"subject_user_id": facts[1] if facts else None},
        held,
    )
    if problems:
        raise PermissionDenied("You can't change your own pay.")


def _parse_amounts(
    components: dict[str, PayComponent], amounts: dict[str, str], errors: dict[str, str]
) -> dict[uuid.UUID, Decimal]:
    """Monthly amounts by component; a percentage component is included with 0 (its
    amount comes from its base). Problems go into `errors`."""
    parsed: dict[uuid.UUID, Decimal] = {}
    for raw_id, text in amounts.items():
        component = components.get(raw_id)
        if component is None or not text.strip():
            continue
        if component.calculation == "percent":
            parsed[component.id] = Decimal(0)
            continue
        try:
            value = parse_money(text, f"amount_{raw_id}")
        except ValidationFailed as exc:
            errors.update(exc.field_errors or {})
            continue
        if value:
            parsed[component.id] = value
    return parsed


def set_pay(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID,
    *,
    pay_group_id: uuid.UUID | None,
    structure_id: uuid.UUID | None,
    start_date: date | None,
    amounts: dict[str, str],  # component id -> monthly amount (fixed) or "on" (percent)
    reason: str,
) -> uuid.UUID:
    """New pay for a person from a date. Pay that started earlier ends the day before;
    pay starting on the same day is replaced."""
    _require(db, actor, PAY_MANAGE, employee_id)
    _check_not_self(db, actor, employee_id)
    if people.status_and_account(db, employee_id) is None:
        raise NotFound()
    errors: dict[str, str] = {}
    group = db.get(PayGroup, pay_group_id) if pay_group_id else None
    if group is None or not group.is_active:
        errors["pay_group_id"] = "Choose a pay group."
    structure = db.get(SalaryStructure, structure_id) if structure_id else None
    if structure_id and structure is None:
        errors["structure_id"] = "Choose a salary structure."
    if start_date is None:
        errors["start_date"] = "Choose the date it applies from."
    components = {
        str(c.id): c for c in db.scalars(select(PayComponent).where(PayComponent.kind == "earning"))
    }
    parsed = _parse_amounts(components, amounts, errors)
    if not any(components[str(cid)].code == "basic" for cid in parsed):
        errors.setdefault("form", "Enter a basic salary.")
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert start_date is not None and group is not None  # noqa: S101
    later = db.scalar(
        select(EmployeePay.id).where(
            EmployeePay.employee_id == employee_id, EmployeePay.start_date > start_date
        )
    )
    if later:
        raise Conflict("This person already has pay starting after that date.")
    same_day = db.scalars(
        select(EmployeePay).where(
            EmployeePay.employee_id == employee_id, EmployeePay.start_date == start_date
        )
    ).first()
    for earlier in db.scalars(
        select(EmployeePay).where(
            EmployeePay.employee_id == employee_id,
            EmployeePay.start_date < start_date,
            or_(EmployeePay.end_date.is_(None), EmployeePay.end_date >= start_date),
        )
    ):
        earlier.end_date = start_date - timedelta(days=1)
    if same_day is not None:
        db.delete(same_day)
        db.flush()
    row = EmployeePay(
        employee_id=employee_id,
        pay_group_id=group.id,
        structure_id=structure.id if structure else None,
        start_date=start_date,
        reason=reason.strip() or None,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    for component_id, amount in parsed.items():
        db.add(EmployeePayAmount(pay_id=row.id, component_id=component_id, amount=amount))
    record_by(
        db,
        actor,
        "payroll.pay.set",
        entity_type="employee",
        entity_id=str(employee_id),
        reason=reason.strip() or None,
        changes=diff_changes(
            {},
            {"pay_group": group.name, "start": start_date.isoformat(), "amounts": "changed"},
            masked_fields=("amounts",),
        ),
    )
    return row.id


@dataclass(frozen=True, slots=True)
class PayRecordView:
    id: uuid.UUID
    pay_group: str
    pay_group_id: uuid.UUID
    structure: str | None
    structure_id: uuid.UUID | None
    start_date: date
    end_date: date | None
    amounts: list[tuple[uuid.UUID, str, str, Decimal]]  # (component id, name, calc, amount)
    monthly_gross: Decimal
    reason: str | None


def pay_history(db: Session, actor: Actor, employee_id: uuid.UUID) -> list[PayRecordView]:
    if not engine.check(db, actor.user_id, PAY_READ, Resource(owner_employee_id=employee_id)):
        raise NotFound()
    records = list(
        db.scalars(
            select(EmployeePay)
            .where(EmployeePay.employee_id == employee_id)
            .order_by(EmployeePay.start_date.desc())
        )
    )
    components = {c.id: c for c in db.scalars(select(PayComponent))}
    groups = {g.id: g.name for g in db.scalars(select(PayGroup))}
    structures = {s.id: s.name for s in db.scalars(select(SalaryStructure))}
    amounts = amounts_of(db, [r.id for r in records])
    views = []
    for r in records:
        own = amounts.get(r.id, {})
        lines = []
        for cid, amount in sorted(own.items(), key=lambda kv: components[kv[0]].sort_order):
            c = components[cid]
            shown = amount
            if c.calculation == "percent" and c.base_component_id:
                base = own.get(c.base_component_id, Decimal(0))
                shown = (base * (c.percent or 0)).quantize(Decimal("0.01"))
            lines.append((cid, c.name, c.calculation, shown))
        views.append(
            PayRecordView(
                r.id,
                groups.get(r.pay_group_id, ""),
                r.pay_group_id,
                structures.get(r.structure_id) if r.structure_id else None,
                r.structure_id,
                r.start_date,
                r.end_date,
                lines,
                monthly_gross(components, own),
                r.reason,
            )
        )
    return views


def current_gross(
    db: Session, employee_ids: list[uuid.UUID], on: date
) -> dict[uuid.UUID, tuple[str, Decimal]]:
    """(pay group, monthly gross) of each person's pay in force on a day, for lists."""
    pays = pay_in_force(db, employee_ids, on)
    components = {c.id: c for c in db.scalars(select(PayComponent))}
    groups = {g.id: g.name for g in db.scalars(select(PayGroup))}
    amounts = amounts_of(db, [p.id for p in pays.values()])
    return {
        eid: (groups.get(p.pay_group_id, ""), monthly_gross(components, amounts.get(p.id, {})))
        for eid, p in pays.items()
    }


# ------------------------------------------------------------------ recurring deductions


def deductions_of(
    db: Session, employee_ids: list[uuid.UUID], month: date
) -> dict[uuid.UUID, list[tuple[str, str, Decimal]]]:
    """Each person's deductions due in a month: (code, name, amount)."""
    if not employee_ids:
        return {}
    components = {c.id: c for c in db.scalars(select(PayComponent))}
    result: dict[uuid.UUID, list[tuple[str, str, Decimal]]] = {}
    for d in db.scalars(
        select(EmployeeDeduction).where(
            EmployeeDeduction.employee_id.in_(employee_ids),
            EmployeeDeduction.start_month <= month,
            or_(EmployeeDeduction.end_month.is_(None), EmployeeDeduction.end_month >= month),
        )
    ):
        c = components[d.component_id]
        result.setdefault(d.employee_id, []).append((c.code, c.name, d.amount))
    return result


def list_deductions(
    db: Session, actor: Actor, employee_id: uuid.UUID
) -> list[tuple[EmployeeDeduction, str]]:
    if not engine.check(db, actor.user_id, PAY_READ, Resource(owner_employee_id=employee_id)):
        raise NotFound()
    components = {c.id: c.name for c in db.scalars(select(PayComponent))}
    return [
        (d, components.get(d.component_id, ""))
        for d in db.scalars(
            select(EmployeeDeduction)
            .where(EmployeeDeduction.employee_id == employee_id)
            .order_by(EmployeeDeduction.start_month.desc())
        )
    ]


def add_deduction(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID,
    *,
    component_id: uuid.UUID | None,
    amount: str,
    start_month: date | None,
    end_month: date | None,
    note: str,
) -> uuid.UUID:
    _require(db, actor, PAY_MANAGE, employee_id)
    _check_not_self(db, actor, employee_id)
    component = db.get(PayComponent, component_id) if component_id else None
    errors = {}
    if component is None or component.kind != "deduction" or not component.is_active:
        errors["deduction_component_id"] = "Choose a deduction."
    if start_month is None:
        errors["start_month"] = "Choose the first month."
    if errors:
        raise ValidationFailed(field_errors=errors)
    value = parse_money(amount, "deduction_amount") or Decimal(0)
    if value <= 0:
        raise ValidationFailed(field_errors={"deduction_amount": "Enter an amount above zero."})
    assert start_month is not None and component is not None  # noqa: S101
    first = start_month.replace(day=1)
    last = end_month.replace(day=1) if end_month else None
    if last is not None and last < first:
        raise ValidationFailed(
            field_errors={"end_month": "The last month can't be before the first."}
        )
    row = EmployeeDeduction(
        employee_id=employee_id,
        component_id=component.id,
        amount=value,
        start_month=first,
        end_month=last,
        note=note.strip()[:200] or None,
        created_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "payroll.deduction.add",
        entity_type="employee",
        entity_id=str(employee_id),
        changes=diff_changes(
            {}, {"deduction": component.name, "amount": "set"}, masked_fields=("amount",)
        ),
    )
    return row.id


def end_deduction(db: Session, actor: Actor, deduction_id: uuid.UUID, *, last_month: date) -> None:
    row = db.get(EmployeeDeduction, deduction_id)
    if row is None:
        raise NotFound()
    _require(db, actor, PAY_MANAGE, row.employee_id)
    _check_not_self(db, actor, row.employee_id)
    last = last_month.replace(day=1)
    if last < row.start_month:
        db.delete(row)
    else:
        row.end_month = last
    record_by(
        db, actor, "payroll.deduction.end", entity_type="employee", entity_id=str(row.employee_id)
    )
