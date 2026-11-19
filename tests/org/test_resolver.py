"""The org resolver: holders, managers, reports, approval chains, vacancies, acting and
interim holders, "as of" dates and cycles (ADR 0015)."""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.core import clock
from app.org import resolver
from app.org.models import Department, Position, PositionAssignment
from tests.org.conftest import Org

pytestmark = pytest.mark.db


def _names(org: Org, ids: set | list) -> list[str]:
    by_id = {v: k for k, v in org.employees.items()}
    return sorted(by_id[i] for i in ids) if isinstance(ids, set) else [by_id[i] for i in ids]


def test_managers_follow_positions(org: Org) -> None:
    db = org.db
    assert _names(org, [resolver.manager_of(db, org.employees["Efe"])]) == ["Gbenga"]
    assert _names(org, [resolver.manager_of(db, org.employees["Bola"])]) == ["Ada"]
    assert _names(org, [resolver.manager_of(db, org.employees["Dayo"])]) == ["Chi"]
    assert resolver.manager_of(db, org.employees["Ada"]) is None


def test_nobody_manages_themselves(org: Org) -> None:
    # Gbenga sits in Payables Clerk, under Finance Manager, which he holds as acting.
    assert _names(org, [resolver.manager_of(org.db, org.employees["Gbenga"])]) == ["Bola"]


def test_approval_chain_runs_to_the_top_without_repeats(org: Org) -> None:
    chain = resolver.approval_chain(org.db, org.employees["Femi"])
    assert _names(org, chain) == ["Gbenga", "Bola", "Ada"]
    assert _names(org, resolver.approval_chain(org.db, org.employees["Femi"], levels=2)) == [
        "Gbenga",
        "Bola",
    ]


def test_reports_direct_and_all_levels(org: Org) -> None:
    db = org.db
    assert _names(org, resolver.reports_of(db, org.employees["Ada"], direct=True)) == [
        "Bola",
        "Chi",
    ]
    assert _names(org, resolver.reports_of(db, org.employees["Ada"])) == [
        "Bola",
        "Chi",
        "Dayo",
        "Efe",
        "Femi",
        "Gbenga",
    ]
    assert _names(org, resolver.reports_of(db, org.employees["Bola"])) == ["Efe", "Femi", "Gbenga"]
    assert _names(org, resolver.reports_of(db, org.employees["Gbenga"])) == ["Efe", "Femi"]
    assert resolver.reports_of(db, org.employees["Efe"]) == set()


def test_vacant_positions_are_skipped_upwards_and_looked_through_downwards(org: Org) -> None:
    acting = next(
        a
        for a in resolver.snapshot(org.db).assignments[org.employees["Gbenga"]]
        if a.kind == "acting"
    )
    # End Gbenga's acting assignment: Finance Manager becomes vacant.
    org.db.execute(
        update(PositionAssignment)
        .where(
            PositionAssignment.position_id == acting.position_id,
            PositionAssignment.kind == "acting",
        )
        .values(end_date=clock.today() - timedelta(days=1))
    )
    org.changed()
    db = org.db
    assert resolver.holder(db, org.positions["Finance Manager"]) is None
    assert _names(org, [resolver.manager_of(db, org.employees["Efe"])]) == ["Bola"]
    assert _names(org, resolver.reports_of(db, org.employees["Bola"], direct=True)) == [
        "Efe",
        "Femi",
        "Gbenga",
    ]


def test_a_primary_holder_wins_over_an_acting_one(org: Org) -> None:
    org.assign("Hauwa", "Finance Manager", "primary")
    db = org.db
    holder = resolver.holder(db, org.positions["Finance Manager"])
    assert holder is not None and holder.employee_id == org.employees["Hauwa"]
    assert _names(org, [resolver.manager_of(db, org.employees["Efe"])]) == ["Hauwa"]
    # Gbenga's acting assignment no longer gives him a team.
    assert resolver.reports_of(db, org.employees["Gbenga"]) == set()


def test_interim_holders_count_only_without_primary_or_acting(org: Org) -> None:
    org.assign("Hauwa", "Head of HR", "interim")
    db = org.db
    assert resolver.holder(db, org.positions["Head of HR"]).employee_id == org.employees["Chi"]
    org.db.execute(
        update(PositionAssignment)
        .where(
            PositionAssignment.position_id == org.positions["Head of HR"],
            PositionAssignment.kind == "primary",
        )
        .values(end_date=clock.today() - timedelta(days=1))
    )
    org.changed()
    assert resolver.holder(db, org.positions["Head of HR"]).employee_id == org.employees["Hauwa"]
    assert _names(org, [resolver.manager_of(db, org.employees["Dayo"])]) == ["Hauwa"]


def test_answers_are_as_of_a_date(org: Org) -> None:
    next_month = clock.today() + timedelta(days=30)
    org.assign("Hauwa", "Finance Manager", "primary", start=next_month)
    db = org.db
    assert (
        resolver.holder(db, org.positions["Finance Manager"]).employee_id == org.employees["Gbenga"]
    )
    assert (
        resolver.holder(db, org.positions["Finance Manager"], next_month).employee_id
        == org.employees["Hauwa"]
    )
    last_year = clock.today() - timedelta(days=365)
    assert resolver.holder(db, org.positions["Managing Director"], last_year) is None


def test_inactive_positions_are_skipped(org: Org) -> None:
    org.db.execute(
        update(Position)
        .where(Position.id == org.positions["Head of Finance"])
        .values(is_active=False)
    )
    org.changed()
    # Finance Manager's parent is inactive, so it becomes a root: Efe's chain stops there.
    assert _names(org, resolver.approval_chain(org.db, org.employees["Efe"])) == ["Gbenga"]


def test_departments_and_sub_departments(org: Org) -> None:
    db = org.db
    fin = org.departments["FIN"]
    assert resolver.departments_under(db, fin) == {fin, org.departments["TRE"]}
    assert resolver.departments_of(db, org.employees["Gbenga"]) == {fin}
    assert _names(org, resolver.department_employees(db, {org.departments["TRE"]})) == ["Femi"]


def test_cycles_in_bad_data_stop_the_walk_and_are_logged(
    org: Org, caplog: pytest.LogCaptureFixture
) -> None:
    # Bypass the service (which refuses this) to simulate corrupted data.
    org.db.execute(
        update(Position)
        .where(Position.id == org.positions["Managing Director"])
        .values(parent_id=org.positions["Accountant"])
    )
    org.db.execute(
        update(Department)
        .where(Department.id == org.departments["FIN"])
        .values(parent_id=org.departments["TRE"])
    )
    org.changed()
    with caplog.at_level(logging.ERROR, logger="app.org"):
        chain = resolver.approval_chain(org.db, org.employees["Efe"])
        resolver.reports_of(org.db, org.employees["Bola"])
        resolver.departments_under(org.db, org.departments["FIN"])
    assert len(chain) == len(set(chain))  # finished, no repeats
    assert "org_cycle_detected" in caplog.text
