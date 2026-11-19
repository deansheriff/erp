"""Connects packages that must not import each other. Called once by each process
entry point (app/main.py, app/worker.py) and by the test suite."""

from app.authz import engine
from app.modules.hr.org_access import PositionTreeResolver


def configure_runtime() -> None:
    # Team and department scopes come from the position tree (ADR 0015).
    engine.set_org_resolver(PositionTreeResolver())
