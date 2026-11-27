"""Assistant settings (ADR 0035), Super Administrators only: the provider, model and API
key, which kinds of data may leave, the limits, and turning it on with an explicit
acceptance. Changing the provider or the kinds of data withdraws the acceptance, which
turns the assistant off until it's given again. Also usage, and testing the connection."""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core import clock
from app.core.errors import Conflict, ValidationFailed
from app.core.secrets import encrypt
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.assistant import common, providers
from app.modules.assistant.models import KINDS, PROVIDERS, AssistantRequest
from app.modules.assistant.run import provider_for

MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,79}$")


@dataclass(frozen=True, slots=True)
class SettingsInput:
    provider: str
    model: str
    api_key: str  # blank keeps the saved key
    kinds: list[str]
    monthly_cap: str
    history_days: str


def _number(text: str, key: str, low: int, high: int, errors: dict[str, str]) -> int:
    try:
        value = int(text.strip().replace(",", ""))
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        errors[key] = f"Enter a number from {low} to {high:,}."
    return value


def save(db: Session, actor: Actor, data: SettingsInput) -> bool:
    """Save the settings. True when the acceptance was withdrawn (provider or data changed)."""
    common.require(db, actor, common.SETTINGS)
    errors: dict[str, str] = {}
    if data.provider not in PROVIDERS:
        errors["provider"] = "Choose a provider."
    model = data.model.strip()
    if not MODEL.fullmatch(model):
        errors["model"] = "Enter the model's id, as the provider names it."
    kinds = [k for k in KINDS if k in data.kinds]
    cap = _number(data.monthly_cap, "monthly_cap", 1, 100000, errors)
    days = _number(data.history_days, "history_days", 1, 3650, errors)
    key = data.api_key.strip()
    if key and (len(key) < 16 or any(c.isspace() for c in key)):
        errors["api_key"] = "That doesn't look like an API key."
    if errors:
        raise ValidationFailed(field_errors=errors)
    row = common.settings(db)
    withdrawn = row.enabled and (
        data.provider != row.accepted_provider or set(kinds) != set(row.accepted_kinds or [])
    )
    row.provider, row.model, row.kinds = data.provider, model, kinds
    row.monthly_cap, row.history_days = cap, days
    if key:
        row.api_key_encrypted, row.api_key_hint = encrypt(key), key[-4:]
    if withdrawn:
        row.enabled = False
        row.accepted_provider = row.accepted_kinds = row.accepted_by = row.accepted_at = None
    db.flush()
    record_by(
        db,
        actor,
        "assistant.settings.update",
        entity_type="assistant_settings",
        entity_id="1",
        changes={
            "provider": data.provider,
            "model": model,
            "kinds": kinds,
            "monthly_cap": cap,
            "history_days": days,
            "api_key_changed": bool(key),
            "acceptance_withdrawn": withdrawn,
        },
    )
    return withdrawn


def turn_on(db: Session, actor: Actor, *, accepted: bool) -> None:
    common.require(db, actor, common.SETTINGS)
    row = common.settings(db)
    if not row.api_key_encrypted:
        raise ValidationFailed(field_errors={"api_key": "Save an API key first."})
    if not row.kinds:
        raise ValidationFailed(field_errors={"kinds": "Tick at least one kind of data to share."})
    if not accepted:
        raise ValidationFailed(
            field_errors={"accept": "Confirm that the ticked data may be sent to the provider."}
        )
    row.enabled = True
    row.accepted_provider, row.accepted_kinds = row.provider, list(row.kinds)
    row.accepted_by, row.accepted_at = actor.user_id, clock.now()
    db.flush()
    record_by(
        db,
        actor,
        "assistant.opt_in",
        entity_type="assistant_settings",
        entity_id="1",
        changes={"provider": row.provider, "model": row.model, "kinds": list(row.kinds)},
    )


def turn_off(db: Session, actor: Actor) -> None:
    common.require(db, actor, common.SETTINGS)
    row = common.settings(db)
    row.enabled = False
    row.accepted_provider = row.accepted_kinds = row.accepted_by = row.accepted_at = None
    db.flush()
    record_by(db, actor, "assistant.opt_out", entity_type="assistant_settings", entity_id="1")


def test_connection(db: Session, actor: Actor) -> str:
    """Send a tiny request that carries no organisation data. Returns a message to show."""
    common.require(db, actor, common.SETTINGS)
    try:
        reply = provider_for(db).generate(
            system="Reply with the JSON requested.",
            prompt='Return {"ok": true}.',
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            name="kx_test",
        )
    except providers.ProviderError as exc:
        raise Conflict(exc.message) from exc
    record_by(db, actor, "assistant.test", entity_type="assistant_settings", entity_id="1")
    if reply.data and reply.data.get("ok") is True:
        return "The provider answered. The connection works."
    raise Conflict("The provider answered, but not as expected. Check the model.")


@dataclass(frozen=True, slots=True)
class UsageRow:
    name: str
    by_feature: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.by_feature.values())


def usage(db: Session, actor: Actor) -> list[UsageRow]:
    """This month's requests by person and feature."""
    common.require(db, actor, common.SETTINGS)
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for user_id, feature, n in db.execute(
        select(AssistantRequest.user_id, AssistantRequest.feature, func.count())
        .where(AssistantRequest.created_at >= common.month_start())
        .group_by(AssistantRequest.user_id, AssistantRequest.feature)
    ).all():
        counts.setdefault(user_id, {})[feature] = int(n)
    names = dict(db.execute(select(User.id, User.full_name).where(User.id.in_(set(counts)))).all())
    rows = [UsageRow(names.get(u, ""), c) for u, c in counts.items()]
    return sorted(rows, key=lambda r: (-r.total, r.name))
