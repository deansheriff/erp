"""Separation of duties as data (brief section 4.2). Services call `enforce`.

A rule says: when performing `permission`, the actor must not be the person named by
`conflict` on the record (its owner, creator, approver, or the subject of the change).
An `override` permission, if set and held, lifts the rule.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Conflict = Literal["subject_is_actor", "owner_is_actor", "creator_is_actor", "approver_is_actor"]
_FACT = {
    "subject_is_actor": "subject_user_id",
    "owner_is_actor": "owner_user_id",
    "creator_is_actor": "created_by",
    "approver_is_actor": "approved_by",
}


@dataclass(frozen=True, slots=True)
class SodRule:
    key: str
    permission: str
    conflict: Conflict
    message: str
    override: str | None = None


def violations(
    rules: tuple[SodRule, ...],
    permission: str,
    actor_user_id: uuid.UUID,
    facts: Mapping[str, uuid.UUID | None],
    held_permissions: frozenset[str],
) -> list[SodRule]:
    """Rules that `actor_user_id` would break by performing `permission` on a record
    described by `facts` (e.g. {"owner_user_id": …, "created_by": …})."""
    broken = []
    for rule in rules:
        if rule.permission != permission:
            continue
        if rule.override and rule.override in held_permissions:
            continue
        if facts.get(_FACT[rule.conflict]) == actor_user_id:
            broken.append(rule)
    return broken
