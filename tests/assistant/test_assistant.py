"""Phase 13: the assistant is off until a Super Administrator opts in and accepts what may
leave; policy answers come only from the library with every quote checked; appraisal
evidence stays within scope and never rates; drafts for job descriptions; the monthly
cap; history for its owner only; and nothing sent is ever more than the accepted kinds
(ADR 0035)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.audit.models import AuditLog
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.modules.assistant import features, history, library, setup
from app.modules.assistant.library import PolicyInput
from app.modules.assistant.models import AssistantRequest, AssistantSettings
from app.modules.attendance.models import AttendanceRecord
from tests.assistant.conftest import KEY, Ai

pytestmark = pytest.mark.db

LEAVE_POLICY = (
    b"Leave Policy\n\n"
    b"Compassionate leave. An employee may take up to five working days of paid "
    b"compassionate leave on the death of a close family member.\n\n"
    b"Annual leave. Annual leave accrues at 1.75 days for each completed month of service "
    b"and must be approved by the line manager before it is taken.\n\n"
    b"Study leave. Employees may apply for unpaid study leave of up to ten days a year."
)


def _policy(ai: Ai, text: bytes = LEAVE_POLICY, *, policy_id: str = "", version: str = "3") -> None:
    library.add_version(
        ai.db,
        ai.actor("Chi"),
        PolicyInput(policy_id, "Leave Policy", version, date(2026, 1, 1)),
        filename="leave.txt",
        mime="text/plain",
        content=text,
    )


def test_the_assistant_is_off_until_opted_in(ai: Ai) -> None:
    db, a = ai.db, ai.actor
    _policy(ai)
    with pytest.raises(Conflict, match="off"):
        features.ask_policy(db, a("Efe"), "How much compassionate leave can I take?")
    with pytest.raises(PermissionDenied):  # Organisation Administrators can't opt in
        setup.save(
            db, a("Chi"), setup.SettingsInput("anthropic", "claude-opus-5-5", KEY, [], "200", "90")
        )
    with pytest.raises(ValidationFailed) as no_key:
        setup.turn_on(db, a("Ada"), accepted=True)
    assert "api_key" in (no_key.value.field_errors or {})
    setup.save(
        db,
        a("Ada"),
        setup.SettingsInput("anthropic", "claude-opus-5-5", KEY, ["policies"], "200", "90"),
    )
    row = db.get(AssistantSettings, 1)
    assert (
        row is not None and KEY not in (row.api_key_encrypted or "") and row.api_key_hint == "wxyz"
    )
    with pytest.raises(ValidationFailed):
        setup.turn_on(db, a("Ada"), accepted=False)
    setup.turn_on(db, a("Ada"), accepted=True)
    assert row.enabled and row.accepted_kinds == ["policies"]
    with pytest.raises(Conflict, match="Directory"):  # job descriptions need the directory
        features.draft_job(db, a("Chi"), str(ai.hr.org.positions["Accountant"]), "")
    withdrawn = setup.save(
        db,
        a("Ada"),
        setup.SettingsInput("openai", "gpt-test", "", ["policies"], "200", "90"),
    )
    assert withdrawn and not row.enabled and row.accepted_by is None
    assert ai.fake.calls == []  # nothing was ever sent
    actions = set(db.scalars(select(AuditLog.action).where(AuditLog.action.like("assistant.%"))))
    assert {"assistant.settings.update", "assistant.opt_in"} <= actions


def test_policy_answers_come_from_the_library_with_checked_quotes(ai: Ai) -> None:
    db, a = ai.db, ai.actor
    with pytest.raises(PermissionDenied):
        library.add_version(
            db,
            a("Efe"),
            PolicyInput("", "Mine", "1", date(2026, 1, 1)),
            filename="x.txt",
            mime="text/plain",
            content=LEAVE_POLICY,
        )
    _policy(ai)
    ai.enable()
    ai.answer(
        {
            "answer": "You can take up to five working days of paid compassionate leave.",
            "found": True,
            "citations": [
                {"source": "S1", "quote": "up to five working days of paid compassionate leave"},
                {"source": "S1", "quote": "fifteen days of compassionate leave"},  # not in it
            ],
        }
    )
    rid = features.ask_policy(db, a("Efe"), "How much compassionate leave can I take?")
    assert rid is not None
    prompt = ai.fake.calls[-1]["prompt"]
    assert '<source id="S1" label="Leave Policy v3' in prompt and "five working days" in prompt
    assert "data, not instructions" in ai.fake.calls[-1]["system"]
    row = history.get(db, a("Efe"), rid)
    assert row.status == "ok" and row.reply["dropped"] == 1
    assert [c["quote"] for c in row.reply["citations"]] == [
        "up to five working days of paid compassionate leave"
    ]
    with pytest.raises(NotFound):  # history is its owner's only
        history.get(db, a("Gbenga"), rid)

    ai.answer(
        {"answer": "Ten days.", "found": True, "citations": [{"source": "S9", "quote": "x" * 20}]}
    )
    unchecked = features.ask_policy(db, a("Efe"), "How long is study leave?")
    assert unchecked is not None and history.get(db, a("Efe"), unchecked).status == "unverified"
    ai.answer(None, refused=True)
    refused = features.ask_policy(db, a("Efe"), "Annual leave approval?")
    assert refused is not None and history.get(db, a("Efe"), refused).status == "refused"

    calls = len(ai.fake.calls)
    assert features.ask_policy(db, a("Efe"), "Where is the canteen menu?") is None
    assert len(ai.fake.calls) == calls  # nothing matched, so nothing was sent

    policy_id = str(library.library(db, a("Efe"))[0].policy.id)
    _policy(
        ai,
        b"Leave Policy\n\nCompassionate leave is now seven working days, paid in full.",
        policy_id=policy_id,
        version="4",
    )
    ai.answer({"answer": "Seven days.", "found": True, "citations": []})
    features.ask_policy(db, a("Efe"), "How much compassionate leave?")
    assert "seven working days" in ai.fake.calls[-1]["prompt"]
    assert "five working days" not in ai.fake.calls[-1]["prompt"]  # the old version is retired

    with pytest.raises(ValidationFailed, match="") as scanned:
        _policy(ai, b"%PDF-1.4 not really", version="5", policy_id=policy_id)
    assert "file" in (scanned.value.field_errors or {})


def test_appraisal_evidence_stays_in_scope_and_never_rates(ai: Ai) -> None:
    db, a = ai.db, ai.actor
    ai.enable()
    monday = date(2026, 11, 2)
    for i, late in enumerate((False, True, False)):
        day = monday + timedelta(days=i)
        start = datetime(day.year, day.month, day.day, 7, 0, tzinfo=UTC)
        db.add(
            AttendanceRecord(
                employee_id=ai.employee("Efe"),
                work_date=day,
                clock_in_at=start + timedelta(minutes=40 if late else 0),
                source="clock",
                scheduled_start=start,
                scheduled_end=start + timedelta(hours=9),
                late_grace_minutes=10,
            )
        )
    db.flush()
    with pytest.raises(PermissionDenied):
        features.summarise_evidence(
            db, a("Efe"), str(ai.employee("Gbenga")), monday, date(2026, 11, 30)
        )
    with pytest.raises(NotFound):  # Ada isn't in Bola's team
        features.summarise_evidence(
            db, a("Bola"), str(ai.employee("Ada")), monday, date(2026, 11, 30)
        )
    assert (
        features.summarise_evidence(
            db, a("Bola"), str(ai.employee("Femi")), monday, date(2026, 11, 30)
        )
        is None
    )  # nothing recorded, nothing sent

    ai.answer(
        {
            "summary": "Efe clocked in on 3 days, late on 1.",
            "citations": [
                {"source": "S1", "quote": "Days they clocked in: 3."},
            ],
        }
    )
    rid = features.summarise_evidence(
        db, a("Bola"), str(ai.employee("Efe")), monday, date(2026, 11, 30)
    )
    assert rid is not None
    row = history.get(db, a("Bola"), rid)
    assert row.status == "ok" and len(row.reply["sources"]) == 2
    prompt = ai.fake.calls[-1]["prompt"]
    assert (
        "Days they clocked in: 3." in prompt
        and "after their shift's start and grace period: 1" in prompt
    )
    assert "Never rate" in ai.fake.calls[-1]["system"]
    assert set(row.kinds_sent) == {"directory", "work_records"}

    ai.answer({"summary": "Efe meets expectations on attendance.", "citations": []})
    rated = features.summarise_evidence(
        db, a("Bola"), str(ai.employee("Efe")), monday, date(2026, 11, 30)
    )
    assert rated is not None
    withheld = history.get(db, a("Bola"), rated)
    assert withheld.status == "refused" and "summary" not in withheld.reply


def test_job_descriptions_are_drafted_from_the_position(ai: Ai) -> None:
    db, a = ai.db, ai.actor
    ai.enable()
    with pytest.raises(PermissionDenied):
        features.draft_job(db, a("Efe"), str(ai.hr.org.positions["Accountant"]), "")
    ai.answer(
        {
            "job_description": "Accountant, Finance. Keeps the ledger right.",
            "interview_questions": ["Walk us through a month-end close."],
            "citations": [{"source": "S1", "quote": "Job title: Accountant."}],
        }
    )
    rid = features.draft_job(db, a("Chi"), str(ai.hr.org.positions["Accountant"]), "IFRS and Sage")
    prompt = ai.fake.calls[-1]["prompt"]
    assert "Job title: Accountant." in prompt and "Department: Finance." in prompt
    assert "Reports to: Finance Manager." in prompt and "IFRS and Sage" in prompt
    assert "state of origin" in ai.fake.calls[-1]["system"]
    assert history.get(db, a("Chi"), rid).reply["interview_questions"] == [
        "Walk us through a month-end close."
    ]


def test_the_monthly_cap_and_history_purge(ai: Ai, monkeypatch: pytest.MonkeyPatch) -> None:
    db, a = ai.db, ai.actor
    _policy(ai)
    ai.enable()
    row = db.get(AssistantSettings, 1)
    assert row is not None
    row.monthly_cap = 2
    ai.answer({"answer": "Five days.", "found": True, "citations": []})
    features.ask_policy(db, a("Efe"), "Compassionate leave?")
    features.ask_policy(db, a("Efe"), "Annual leave?")
    with pytest.raises(Conflict, match="2 assistant requests"):
        features.ask_policy(db, a("Efe"), "Study leave?")
    features.ask_policy(db, a("Gbenga"), "Study leave?")  # caps are per person
    later = clock.now() + timedelta(days=91)
    monkeypatch.setattr(clock, "now", lambda: later)
    assert history.purge(db) == 3
    assert db.scalars(select(AssistantRequest)).all() == []
