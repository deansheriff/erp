"""Permission and scope types shared by every package's policies.py (ADR 0013)."""

import re
from dataclasses import dataclass
from enum import StrEnum

_KEY = re.compile(r"^[a-z_]+(\.[a-z_]+){2,3}$")


class Scope(StrEnum):
    """How far a grant reaches, narrowest first."""

    SELF = "self"
    TEAM = "team"
    DEPARTMENT = "department"
    ORGANIZATION = "organization"

    @property
    def rank(self) -> int:
        return list(Scope).index(self)

    @property
    def label(self) -> str:
        return {
            "self": "Self",
            "team": "Team",
            "department": "Department",
            "organization": "Organisation",
        }[self.value]


@dataclass(frozen=True, slots=True)
class Permission:
    """One action on one resource: `module.resource[.subresource].action`."""

    key: str
    label: str
    description: str = ""
    sensitive: bool = False
    # Narrowest-to-widest scopes this permission makes sense at (e.g. settings are
    # organisation-only). Grants outside this list are rejected.
    scopes: tuple[Scope, ...] = tuple(Scope)

    def __post_init__(self) -> None:
        if not _KEY.match(self.key):
            raise ValueError(f"Bad permission key {self.key!r}: use module.resource[.sub].action")

    @property
    def module(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def is_read(self) -> bool:
        return self.key.endswith(".read")


ORG_ONLY = (Scope.ORGANIZATION,)
