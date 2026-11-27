"""Phase 11e: tickets routed to a team by category, taken or assigned, public replies and
internal notes, waiting pauses the clock, resolution, rating and reopening, customer
tickets, business-hours SLA targets with warnings and breaches, closing, and the SLA
report (ADR 0032)."""

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.mail.models import MailOutbox
from app.modules.finance import customers
from app.modules.finance.customers import CustomerInput
from app.modules.support import report, setup, sla, tickets, watch
from app.modules.support.models import Ticket
from app.modules.support.tickets import TicketInput
from tests.support.conftest import LAGOS, Desk, at

pytestmark = pytest.mark.db


def _raise(d: Desk, who: str = "Hauwa", priority: str = "high", **extra: str) -> uuid.UUID:
    return tickets.raise_ticket(
        d.db,
        d.actor(who),
        TicketInput(
            extra.pop("category", d.category("Network")),
            priority,
            "Laptop won't connect to VPN",
            "Since this morning the VPN times out.",
            **extra,
        ),
    )


def _row(d: Desk, ticket: uuid.UUID) -> Ticket:
    row = d.db.get(Ticket, ticket)
    assert row is not None
    return row


def _mails(d: Desk, template: str) -> list[MailOutbox]:
    return list(d.db.scalars(select(MailOutbox).where(MailOutbox.template == template)))


def test_business_hours_skip_nights_weekends_and_holidays() -> None:
    def none(_a: date, _b: date) -> set[date]:
        return set()

    hours = {"day_start": at(2026, 1, 1, 8).time(), "day_end": at(2026, 1, 1, 17).time()}
    friday = at(2026, 11, 27, 16, 30)
    assert sla.add_minutes(friday, 60, holidays=none, **hours).astimezone(LAGOS) == at(
        2026, 11, 30, 8, 30
    )
    early = at(2026, 11, 23, 6)
    assert sla.add_minutes(early, 30, holidays=none, **hours).astimezone(LAGOS) == at(
        2026, 11, 23, 8, 30
    )

    def monday_off(_a: date, _b: date) -> set[date]:
        return {date(2026, 11, 30)}

    assert sla.add_minutes(friday, 60, holidays=monday_off, **hours).astimezone(LAGOS) == at(
        2026, 12, 1, 8, 30
    )
    assert sla.minutes_between(friday, at(2026, 11, 30, 9), holidays=none, **hours) == 90
    assert sla.describe(1620, 540) == "3 days" and sla.describe(135, 540) == "2h 15m"


def test_tickets_go_to_the_team_and_are_worked(desk: Desk) -> None:
    d, a = desk, desk.actor
    ticket = _raise(d)
    row = _row(d, ticket)
    assert (row.number, row.team_id, row.status) == ("TKT-00001", d.it, "open")
    assert row.response_due_at.astimezone(LAGOS) == at(2026, 11, 23, 13)  # 4 business hours
    assert row.resolve_due_at.astimezone(LAGOS) == at(2026, 11, 24, 9)  # 9 business hours
    assert [r.ticket.id for r in tickets.list_tickets(d.db, a("Hauwa"), "mine")] == [ticket]
    assert [r.ticket.id for r in tickets.list_tickets(d.db, a("Dayo"), "queue")] == [ticket]
    assert tickets.list_tickets(d.db, a("Efe"), "queue") == []
    with pytest.raises(NotFound):
        tickets.get(d.db, a("Efe"), ticket)  # another team's ticket

    with pytest.raises(PermissionDenied):
        tickets.assign(d.db, a("Dayo"), ticket, str(d.employee("Dayo")))  # not the lead
    with pytest.raises(ValidationFailed):
        tickets.assign(d.db, a("Chi"), ticket, str(d.employee("Efe")))  # not on the team
    tickets.assign(d.db, a("Chi"), ticket, str(d.employee("Dayo")))
    assert _row(d, ticket).status == "in_progress"

    tickets.reply(d.db, a("Dayo"), ticket, body="Checked the logs.", internal=True)
    assert all(not e.comment.internal for e in tickets.get(d.db, a("Hauwa"), ticket).entries)
    with pytest.raises(PermissionDenied):
        tickets.reply(d.db, a("Hauwa"), ticket, body="Note", internal=True)
    d.set_time(at(2026, 11, 23, 10))
    tickets.reply(
        d.db,
        a("Dayo"),
        ticket,
        body="Please restart and try again.",
        internal=False,
        then="waiting",
    )
    row = _row(d, ticket)
    assert row.status == "waiting" and row.first_response_at is not None
    assert len(_mails(d, "support_ticket_update")) == 1

    d.set_time(at(2026, 11, 23, 14))  # four business hours waiting
    tickets.reply(d.db, a("Hauwa"), ticket, body="Still failing.", internal=False)
    row = _row(d, ticket)
    assert (row.status, row.paused_minutes) == ("in_progress", 240)
    assert row.resolve_due_at.astimezone(LAGOS) == at(2026, 11, 24, 13)  # pushed by 4 hours

    with pytest.raises(ValidationFailed):
        tickets.resolve(d.db, a("Dayo"), ticket, resolution="")
    tickets.resolve(d.db, a("Dayo"), ticket, resolution="Reinstalled the VPN client.")
    assert _row(d, ticket).status == "resolved"
    assert len(_mails(d, "support_ticket_update")) == 2
    with pytest.raises(PermissionDenied):
        tickets.rate(d.db, a("Dayo"), ticket, score="5", comment="")
    tickets.rate(d.db, a("Hauwa"), ticket, score="5", comment="Quick, thanks")
    tickets.reopen(d.db, a("Hauwa"), ticket)
    assert _row(d, ticket).rating is None
    tickets.resolve(d.db, a("Dayo"), ticket, resolution="Replaced the network card.")


def test_sla_warnings_breaches_and_closing(desk: Desk) -> None:
    d, a = desk, desk.actor
    ticket = _raise(d, priority="urgent")  # 1 hour to respond, 4 to resolve: due 13:00
    tickets.take(d.db, a("Dayo"), ticket)
    d.set_time(at(2026, 11, 23, 12, 5))  # past 75% of four hours
    assert watch.run(d.db).warned == 1
    assert watch.run(d.db).warned == 0  # once
    assert _row(d, ticket).response_breached  # nobody replied within the hour
    d.set_time(at(2026, 11, 23, 13, 30))
    assert watch.run(d.db).breached == 1
    alerts = _mails(d, "support_sla_alert")
    assert len(alerts) == 2 and "breached" in alerts[-1].subject
    assert tickets.clock_of(d.db, _row(d, ticket)).state == "breached"

    tickets.resolve(d.db, a("Dayo"), ticket, resolution="Fixed the router.")
    assert _row(d, ticket).resolve_breached
    d.set_time(at(2026, 11, 26, 13))  # half an hour short of three business days
    assert watch.run(d.db).closed == 0
    d.set_time(at(2026, 11, 26, 14))  # past three business days
    assert watch.run(d.db).closed == 1
    assert _row(d, ticket).status == "closed"
    with pytest.raises(Conflict):
        tickets.reply(d.db, a("Hauwa"), ticket, body="Again", internal=False)

    numbers = report.build(d.db, a("Dayo"), date(2026, 11, 1))
    assert (numbers.total.tickets, numbers.total.resolve_met) == (1, 0)
    assert numbers.by_team[0][:2] == ("IT", "urgent")


def test_customer_tickets_are_recorded_by_agents(desk: Desk) -> None:
    d, a = desk, desk.actor
    customer = customers.save(
        d.db,
        a("Ada"),
        None,
        CustomerInput(
            "Adebayo Foods Ltd", "accounts@adebayo.example", "", "", "", "30", True, True
        ),
    )
    with pytest.raises(PermissionDenied):
        _raise(d, "Hauwa", customer_id=str(customer))
    ticket = _raise(d, "Dayo", priority="normal", customer_id=str(customer))
    row = _row(d, ticket)
    assert row.requester_employee_id is None and row.contact_email == "accounts@adebayo.example"
    tickets.reply(d.db, a("Dayo"), ticket, body="We're on it.", internal=False)
    mail = _mails(d, "support_ticket_update")[0]
    assert mail.to_address == "accounts@adebayo.example"


def test_settings_change_targets_and_hours(desk: Desk) -> None:
    d, a = desk, desk.actor
    data = setup.SettingsInput(
        "08:00",
        "16:00",
        "80",
        "5",
        {"low": "16", "normal": "8", "high": "4", "urgent": "1"},
        {"low": "40", "normal": "24", "high": "2", "urgent": "4"},
    )
    with pytest.raises(PermissionDenied):
        setup.save_settings(d.db, a("Dayo"), data)
    with pytest.raises(ValidationFailed) as bad:
        setup.save_settings(d.db, a("Ada"), data)  # high resolves before it responds
    assert "resolve_high" in (bad.value.field_errors or {})
    setup.save_settings(
        d.db,
        a("Ada"),
        setup.SettingsInput(
            "08:00", "16:00", "80", "5", data.response_hours, {**data.resolve_hours, "high": "8"}
        ),
    )
    assert setup.day_minutes(d.db) == 480
    assert setup.targets(d.db)["high"].resolve_minutes == 480
    assert setup.settings(d.db).close_after_days == 5
    assert tickets.business_minutes(d.db, at(2026, 11, 23, 8), at(2026, 11, 24, 8)) == 480
    assert timedelta(minutes=480)
