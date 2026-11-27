"""What other modules may use from fleet (ADR 0033): the link finance names vehicles
through. `app/wiring.py` hands `LINK` to finance at start-up."""

from app.modules.fleet.vehicles import Link

LINK = Link()

__all__ = ["LINK"]
