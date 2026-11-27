"""Ticket suggestions (ADR 0035): only for agents of the ticket's team; the category is
always one that exists; the reply is a draft; internal notes are never sent."""

import pytest

from app.core.errors import NotFound, PermissionDenied
from app.identity.models import User
from app.modules.assistant import features, history, providers, setup
from app.modules.assistant.providers import Reply
from app.modules.support import tickets
from app.modules.support.tickets import TicketInput
from tests.assistant.conftest import KEY, FakeProvider
from tests.support.conftest import Desk, desk  # noqa: F401  (re-exported fixture)

pytestmark = pytest.mark.db


def test_suggestions_are_for_the_tickets_agents_only(desk: Desk) -> None:  # noqa: F811
    db, a = desk.db, desk.actor
    ada = db.get(User, desk.hr.org.users["Ada"])
    assert ada is not None
    desk.hr.people.give(ada, "super_administrator")
    fake = FakeProvider()
    providers.set_provider(fake)
    try:
        setup.save(
            db,
            a("Ada"),
            setup.SettingsInput(
                "anthropic", "claude-opus-5-5", KEY, ["support_tickets"], "200", "90"
            ),
        )
        setup.turn_on(db, a("Ada"), accepted=True)
        ticket = tickets.raise_ticket(
            db,
            a("Hauwa"),
            TicketInput(desk.category("Network"), "high", "VPN drops", "Every ten minutes."),
        )
        tickets.take(db, a("Dayo"), ticket)
        tickets.reply(db, a("Dayo"), ticket, body="Router logs look odd.", internal=True, then="")
        with pytest.raises(PermissionDenied):  # Hauwa isn't a support agent
            features.suggest_for_ticket(db, a("Hauwa"), ticket)
        with pytest.raises(NotFound):  # Efe works the HR team's tickets, not IT's
            features.suggest_for_ticket(db, a("Efe"), ticket)

        fake.reply = lambda *_: Reply(
            {
                "category": "Plumbing",
                "reason": "Water.",
                "reply": "Hello, try the new client.",
                "citations": [],
            }
        )
        rid = features.suggest_for_ticket(db, a("Dayo"), ticket)
        row = history.get(db, a("Dayo"), rid)
        assert row.reply["category"] == "Network"  # an unknown category isn't kept
        assert row.reply["reply"] == "Hello, try the new client."
        call = fake.calls[-1]
        assert "VPN drops" in call["prompt"] and "Router logs" not in call["prompt"]
        assert set(call["schema"]["properties"]["category"]["enum"]) == {"Network", "Payroll query"}
    finally:
        providers.set_provider(None)
