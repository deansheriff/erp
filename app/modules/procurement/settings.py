"""Procurement settings (ADR 0030): the approval tiers by amount and the amount over which a
requisition needs three quotes. All seeded values are to confirm."""

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.authz import engine
from app.core.errors import PermissionDenied, ValidationFailed
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.procurement.models import LEVELS, ApprovalTier, ProcurementSettings

MANAGE = "procurement.settings.manage"
LEVEL_LABELS = {
    "line_manager": "Line manager",
    "finance": "Finance Manager",
    "director": "Director",
}
ZERO = Decimal(0)
CENT = Decimal("0.01")
MIN_QUOTES = 3


def can(db: Session, actor: Actor, permission: str) -> bool:
    return bool(engine.check(db, actor.user_id, permission))


def require(db: Session, actor: Actor, permission: str) -> None:
    if not can(db, actor, permission):
        raise PermissionDenied()


def uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def parse_money(
    text: str, key: str, errors: dict[str, str], *, label: str = "an amount"
) -> Decimal:
    try:
        value = Decimal((text or "").strip().replace(",", "").replace("₦", ""))
    except InvalidOperation:
        value = Decimal(-1)
    if not value.is_finite() or value < 0 or value != value.quantize(CENT):
        errors[key] = f"Enter {label}, like 250,000.00."
        return ZERO
    return value


def user_names(db: Session, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(wanted))).all())


def settings(db: Session) -> ProcurementSettings:
    row = db.get(ProcurementSettings, 1)
    if row is None:  # seeded by the migration; recreated if it ever goes missing
        row = ProcurementSettings(id=1, quote_threshold=Decimal(1_000_000))
        db.add(row)
        db.flush()
    return row


def tiers(db: Session) -> list[ApprovalTier]:
    rows = {t.level: t for t in db.scalars(select(ApprovalTier))}
    return [rows[level] for level in LEVELS if level in rows]


def levels_for(db: Session, amount: Decimal) -> list[str]:
    """The approval steps a requisition of this net total needs, in order. The line manager
    always decides; each further active tier joins when the total is over its amount."""
    out = []
    for tier in tiers(db):
        if tier.level == "line_manager" or (tier.is_active and amount > tier.above):
            out.append(tier.level)
    return out


def quotes_needed(db: Session, amount: Decimal) -> int:
    return MIN_QUOTES if amount > settings(db).quote_threshold else 1


@dataclass(frozen=True, slots=True)
class SettingsInput:
    quote_threshold: str
    above: dict[str, str]  # by level
    active: dict[str, bool]


def save(db: Session, actor: Actor, data: SettingsInput) -> None:
    require(db, actor, MANAGE)
    errors: dict[str, str] = {}
    threshold = parse_money(data.quote_threshold, "quote_threshold", errors)
    rows = tiers(db)
    amounts = {}
    for tier in rows:
        if tier.level == "line_manager":
            continue
        amounts[tier.level] = parse_money(
            data.above.get(tier.level, ""), f"above_{tier.level}", errors
        )
    if (
        not errors
        and data.active.get("finance")
        and data.active.get("director")
        and amounts["director"] <= amounts["finance"]
    ):
        errors["above_director"] = "The Director's amount must be over the Finance Manager's."
    if errors:
        raise ValidationFailed(field_errors=errors)
    before = {t.level: (str(t.above), t.is_active) for t in rows}
    row = settings(db)
    old_threshold = row.quote_threshold
    row.quote_threshold = threshold
    for tier in rows:
        if tier.level != "line_manager":
            tier.above, tier.is_active = amounts[tier.level], bool(data.active.get(tier.level))
    db.flush()
    record_by(
        db,
        actor,
        "procurement.settings.update",
        entity_type="procurement_settings",
        entity_id="1",
        changes={
            "quote_threshold": {"from": str(old_threshold), "to": str(threshold)},
            "tiers": {
                "from": before,
                "to": {t.level: (str(t.above), t.is_active) for t in rows},
            },
        },
    )
