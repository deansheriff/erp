"""What other modules may use from projects (ADR 0031): the link finance names projects
through. `app/wiring.py` hands `LINK` to finance at start-up."""

from app.modules.projects.projects import Link

LINK = Link()

__all__ = ["LINK"]
