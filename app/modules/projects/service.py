"""What other modules may use from projects (ADR 0031): the link finance names projects
through, and facts for the assistant's appraisal evidence (ADR 0035). `app/wiring.py`
hands `LINK` to finance at start-up."""

import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.modules.projects import evidence
from app.modules.projects.projects import Link

LINK = Link()


def evidence_facts(
    db: Session, employee_id: uuid.UUID, start: date, end: date
) -> list[tuple[str, str]]:
    """Plain facts for the assistant's appraisal evidence (ADR 0035)."""
    return evidence.facts(db, employee_id, start, end)


__all__ = ["LINK", "evidence_facts"]
