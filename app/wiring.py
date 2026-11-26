"""Connects packages that must not import each other. Called once by each process
entry point (app/main.py, app/worker.py) and by the test suite."""

from app.authz import engine
from app.modules.finance import service as finance
from app.modules.hr.org_access import PositionTreeResolver
from app.modules.inventory import service as inventory
from app.modules.projects import service as projects


def configure_runtime() -> None:
    # Team and department scopes come from the position tree (ADR 0015).
    engine.set_org_resolver(PositionTreeResolver())
    # Finance moves stock through inventory without importing it (ADR 0029).
    finance.set_link(inventory.LINK)
    # Finance and the modules posting through it name projects the same way (ADR 0031).
    finance.set_project_link(projects.LINK)
