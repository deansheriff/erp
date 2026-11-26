"""Suppliers (ADR 0025): a company or an individual (which picks the WHT rate), payment
terms, a default WHT type, and the bank details the payment file uses."""

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core import numbering
from app.core.errors import NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import payables, periods
from app.modules.finance.models import SUPPLIER_KINDS, Supplier, WhtType

READ = "finance.supplier.read"
MANAGE = "finance.supplier.manage"
SEQUENCE = "supplier"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NUBAN = re.compile(r"^\d{10}$")
BANK_CODE = re.compile(r"^\d{3,6}$")
MAX_TERMS = 365
SHOWS = ("active", "inactive", "overdue")
KIND_LABELS = {"company": "Company", "individual": "Individual"}


def _require(db: Session, actor: Actor, permission: str) -> None:
    if not engine.check(db, actor.user_id, permission):
        raise PermissionDenied()


@dataclass(frozen=True, slots=True)
class SupplierInput:
    name: str
    kind: str
    email: str
    phone: str
    address: str
    tin: str
    payment_terms_days: str
    wht_type_id: str
    bank_name: str
    bank_code: str
    account_number: str
    account_name: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class SupplierRow:
    id: uuid.UUID
    code: str
    name: str
    kind: str
    tin: str | None
    payment_terms_days: int
    bank: str | None
    is_active: bool
    balance: Decimal
    overdue: Decimal


def masked_account(supplier: Supplier) -> str | None:
    if not supplier.account_number:
        return None
    return f"{supplier.bank_name or 'Bank'} ••••{supplier.account_number[-4:]}"


def list_suppliers(
    db: Session, actor: Actor, *, q: str = "", show: str = "active", today: date
) -> list[SupplierRow]:
    _require(db, actor, READ)
    query = select(Supplier).order_by(Supplier.name)
    query = query.where(Supplier.is_active.is_(show != "inactive"))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            or_(Supplier.name.ilike(like), Supplier.code.ilike(like), Supplier.tin.ilike(like))
        )
    rows = list(db.scalars(query.limit(500)))
    money = payables.balances(db, [s.id for s in rows], today=today)
    out = [
        SupplierRow(
            s.id,
            s.code,
            s.name,
            s.kind,
            s.tin,
            s.payment_terms_days,
            masked_account(s),
            s.is_active,
            *money[s.id],
        )
        for s in rows
    ]
    return [r for r in out if r.overdue > 0] if show == "overdue" else out


def choices(db: Session) -> list[tuple[uuid.UUID, str]]:
    return [
        (s.id, f"{s.code} · {s.name}")
        for s in db.scalars(select(Supplier).where(Supplier.is_active).order_by(Supplier.name))
    ]


def get(db: Session, actor: Actor, supplier_id: uuid.UUID) -> Supplier:
    _require(db, actor, READ)
    row = db.get(Supplier, supplier_id)
    if row is None:
        raise NotFound()
    return row


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _check(
    db: Session, supplier_id: uuid.UUID | None, data: SupplierInput
) -> tuple[dict[str, str], int]:
    errors: dict[str, str] = {}
    name = data.name.strip()
    if not name:
        errors["name"] = "Enter the supplier's name."
    else:
        clash = select(Supplier.id).where(func.lower(Supplier.name) == name.lower())
        if supplier_id:
            clash = clash.where(Supplier.id != supplier_id)
        if db.scalar(clash):
            errors["name"] = "A supplier already has that name."
    if data.kind not in SUPPLIER_KINDS:
        errors["kind"] = "Choose company or individual."
    email = data.email.strip()
    if email and not EMAIL.match(email):
        errors["email"] = "Enter an email address, like accounts@example.com."
    try:
        terms = int(data.payment_terms_days.strip() or periods.settings(db).default_terms_days)
    except ValueError:
        terms = -1
    if not 0 <= terms <= MAX_TERMS:
        errors["payment_terms_days"] = f"Enter days from 0 to {MAX_TERMS}."
    wht = _uuid(data.wht_type_id)
    if data.wht_type_id and (wht is None or db.get(WhtType, wht) is None):
        errors["wht_type_id"] = "Choose a withholding tax type."
    errors.update(_bank_errors(data))
    return errors, terms


def _bank_errors(data: SupplierInput) -> dict[str, str]:
    """Bank details are optional, but all four go together."""
    fields = {
        "bank_name": data.bank_name.strip(),
        "bank_code": data.bank_code.strip(),
        "account_number": data.account_number.strip(),
        "account_name": data.account_name.strip(),
    }
    if not any(fields.values()):
        return {}
    errors = {}
    if not fields["bank_name"]:
        errors["bank_name"] = "Enter the bank's name."
    if not BANK_CODE.match(fields["bank_code"]):
        errors["bank_code"] = "Enter the bank's CBN code, 3 to 6 digits."
    if not NUBAN.match(fields["account_number"]):
        errors["account_number"] = "Enter the 10-digit account number."
    if not fields["account_name"]:
        errors["account_name"] = "Enter the name on the account."
    return errors


def save(
    db: Session, actor: Actor, supplier_id: uuid.UUID | None, data: SupplierInput
) -> uuid.UUID:
    _require(db, actor, MANAGE)
    errors, terms = _check(db, supplier_id, data)
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = db.get(Supplier, supplier_id) if supplier_id else None
    if supplier_id and row is None:
        raise NotFound()
    if row is None:
        row = Supplier(
            code=numbering.next_number(db, SEQUENCE),
            name=data.name.strip(),
            payment_terms_days=terms,
        )
        db.add(row)
    keys = ("name", "kind", "payment_terms_days", "bank_code", "account_number", "is_active")
    before = {k: getattr(row, k, None) for k in keys}
    row.name, row.kind = data.name.strip(), data.kind
    row.email = data.email.strip().lower() or None
    row.phone = data.phone.strip()[:40] or None
    row.address = data.address.strip() or None
    row.tin = data.tin.strip()[:30] or None
    row.payment_terms_days = terms
    row.wht_type_id = _uuid(data.wht_type_id)
    row.bank_name = data.bank_name.strip()[:120] or None
    row.bank_code = data.bank_code.strip() or None
    row.account_number = data.account_number.strip() or None
    row.account_name = data.account_name.strip()[:200] or None
    row.is_active = data.is_active
    db.flush()
    after = {k: getattr(row, k) for k in keys}
    record_by(
        db,
        actor,
        "finance.supplier.save",
        entity_type="supplier",
        entity_id=str(row.id),
        changes={k: {"from": before[k], "to": v} for k, v in after.items() if before[k] != v},
    )
    return row.id
