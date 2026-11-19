"""The permission catalogue: every package's `policies.py`, collected in one place.

Add a package's policies module to POLICY_MODULES when it declares permissions. The app
refuses to start if this catalogue and the `permissions` table differ (ADR 0013).
"""

from functools import cache
from importlib import import_module

from app.authz.permissions import Permission
from app.authz.sod import SodRule

POLICY_MODULES = (
    "app.identity.policies",
    "app.mail.policies",
    "app.org.policies",
    "app.audit.policies",
    "app.authz.policies",
    "app.files.policies",
    "app.modules.hr.policies",
)


@cache
def permissions() -> dict[str, Permission]:
    found: dict[str, Permission] = {}
    for name in POLICY_MODULES:
        for permission in import_module(name).PERMISSIONS:
            if permission.key in found:
                raise ValueError(f"Permission {permission.key} is declared twice")
            found[permission.key] = permission
    return dict(sorted(found.items()))


@cache
def sod_rules() -> tuple[SodRule, ...]:
    rules: list[SodRule] = []
    for name in POLICY_MODULES:
        rules.extend(import_module(name).SOD_RULES)
    return tuple(rules)


def get(key: str) -> Permission:
    try:
        return permissions()[key]
    except KeyError as exc:
        raise KeyError(f"Unknown permission {key!r}: declare it in a policies.py") from exc
