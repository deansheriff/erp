"""The catalogue, templates and field classes, without a database."""

from pathlib import Path

import yaml
from fastapi.routing import APIRoute

from app.authz import catalogue
from app.authz.declarations import declared_permission
from app.authz.fields import FieldClass, masked_fields, read_permission, redact
from app.authz.permissions import Scope
from app.authz.role_templates import TEMPLATES
from app.core.config import get_settings
from app.main import routers

MATRIX = Path(__file__).with_name("permission_matrix.yaml")


def test_role_templates_match_the_reviewed_snapshot() -> None:
    """A failure means a template's grants changed. Review the change, then run
    `make permissions-doc` to update the snapshot and docs/PERMISSIONS.md."""
    expected = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
    actual = {t.key: {k: s.value for k, s in t.resolve().items()} for t in TEMPLATES}
    assert actual == expected


def test_organisation_admin_never_gets_technical_settings() -> None:
    org_admin = next(t for t in TEMPLATES if t.key == "organization_administrator")
    assert not [k for k in org_admin.resolve() if k.startswith("system.")]


def test_auditor_only_reads() -> None:
    auditor = next(t for t in TEMPLATES if t.key == "auditor")
    assert all(k.endswith(".read") for k in auditor.resolve())
    assert "audit.log.read" in auditor.resolve()


def test_every_route_permission_is_in_the_catalogue() -> None:
    routes = [
        r for router in routers(get_settings()) for r in router.routes if isinstance(r, APIRoute)
    ]
    unknown = {
        f"{r.path}: {p}"
        for r in routes
        if (p := declared_permission(r.endpoint)) is not None and p not in catalogue.permissions()
    }
    assert not unknown


def test_every_permission_allows_at_least_one_scope() -> None:
    assert all(p.scopes and set(p.scopes) <= set(Scope) for p in catalogue.permissions().values())


def test_sod_rules_refer_to_real_permissions() -> None:
    perms = catalogue.permissions()
    for rule in catalogue.sod_rules():
        assert rule.permission in perms
        assert rule.override is None or rule.override in perms


def test_read_permissions_per_field_class() -> None:
    assert read_permission("hr.employee", FieldClass.DIRECTORY) == "hr.employee.read"
    assert read_permission("hr.employee", FieldClass.BANKING) == "hr.employee.banking.read"


def test_redaction_hides_unclassified_fields() -> None:
    classes = {"name": FieldClass.DIRECTORY, "salary": FieldClass.COMPENSATION}
    data = {"name": "Amaka Eze", "salary": 450_000, "bvn": "22123456789"}
    assert redact(data, classes, {FieldClass.DIRECTORY}) == {"name": "Amaka Eze"}
    everything_but_sensitive = set(FieldClass) - {FieldClass.SENSITIVE}
    assert redact(data, classes, everything_but_sensitive) == {
        "name": "Amaka Eze",
        "salary": 450_000,
    }


def test_money_and_identity_fields_are_masked_in_the_audit_log() -> None:
    classes = {
        "name": FieldClass.DIRECTORY,
        "phone": FieldClass.PERSONAL,
        "salary": FieldClass.COMPENSATION,
        "account_number": FieldClass.BANKING,
        "nin": FieldClass.STATUTORY,
    }
    assert masked_fields(classes) == {"salary", "account_number", "nin"}
