"""Attendance (ADR 0019): clocking in and out, lateness and early leaving with grace,
overtime approval, the geofence and photo rules, missing clock-outs and corrections, night
shifts, the roster, leave and holidays, and the monthly summary."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.models import StoredFile
from app.modules.attendance import service, settings
from app.modules.attendance.models import AttendanceRecord, OvertimeEntry, ShiftPattern
from app.modules.attendance.schemas import DayView, Position
from app.modules.leave.models import LeaveRequest
from tests.attendance.conftest import HEAD_OFFICE, MONDAY, Crew, lagos
from tests.leave.conftest import leave_type

pytestmark = pytest.mark.db

TUESDAY = MONDAY + timedelta(days=1)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 200
AT_OFFICE = Position(Decimal("6.428300"), Decimal("3.421900"), 12)  # about 22 m away
ACROSS_TOWN = Position(Decimal("6.437100"), Decimal("3.421900"), 12)  # about 1 km away


def _day(crew: Crew, who: str, day: date) -> DayView:
    return service.days_for(crew.hr.db, [crew.hr.employee(who)], day, day)[crew.hr.employee(who)][0]


def _clock(
    crew: Crew, who: str, hour: int, minute: int, *, out: bool = False, day: date = MONDAY
) -> None:
    crew.set_time(lagos(day, hour, minute))
    if out:
        service.clock_out(crew.hr.db, crew.hr.actor(who), position=None)
    else:
        service.clock_in(crew.hr.db, crew.hr.actor(who), position=None)
    crew.hr.db.flush()


def _geofence(crew: Crew, *, photo: bool = False) -> None:
    settings.save_location(
        crew.hr.db,
        crew.hr.actor("Chi"),
        crew.branch_id,  # type: ignore[arg-type]
        geofence_enabled=True,
        latitude=str(HEAD_OFFICE[0]),
        longitude=str(HEAD_OFFICE[1]),
        radius_m="150",
        require_photo=photo,
    )


# ------------------------------------------------------------------ a normal day


def test_on_time_day_counts_hours_less_the_break(crew: Crew) -> None:
    _clock(crew, "Efe", 7, 58)
    assert _day(crew, "Efe", MONDAY).status == "working"
    with pytest.raises(Conflict, match="already clocked in"):
        _clock(crew, "Efe", 8, 30)
    _clock(crew, "Efe", 17, 0, out=True)
    d = _day(crew, "Efe", MONDAY)
    assert d.status == "done"
    assert (d.late_minutes, d.early_minutes, d.overtime_minutes) == (0, 0, 0)
    assert d.worked_minutes == 9 * 60 + 2 - 60
    with pytest.raises(Conflict, match="not clocked in"):
        _clock(crew, "Efe", 17, 5, out=True)


@pytest.mark.parametrize(
    ("clock_in", "clock_out", "late", "early"),
    [
        ((8, 15), (16, 45), 0, 0),  # exactly at the grace limits
        ((8, 16), (16, 44), 16, 16),  # just past them: counted from the shift times
    ],
)
def test_lateness_and_early_leaving_respect_the_grace(
    crew: Crew, clock_in: tuple[int, int], clock_out: tuple[int, int], late: int, early: int
) -> None:
    _clock(crew, "Efe", *clock_in)
    _clock(crew, "Efe", *clock_out, out=True)
    d = _day(crew, "Efe", MONDAY)
    assert (d.late_minutes, d.early_minutes) == (late, early)


def test_grace_is_set_per_pattern(crew: Crew) -> None:
    settings.save_pattern(
        crew.hr.db,
        crew.hr.actor("Chi"),
        crew.day_shift.id,
        name="Day shift",
        start_time="08:00",
        end_time="17:00",
        break_minutes="60",
        weekdays=["0", "1", "2", "3", "4"],
        late_grace="5",
        early_grace="15",
        is_active=True,
    )
    _clock(crew, "Efe", 8, 6)
    assert _day(crew, "Efe", MONDAY).late_minutes == 6


def test_clocking_back_in_keeps_the_first_clock_in(crew: Crew) -> None:
    _clock(crew, "Efe", 8, 0)
    _clock(crew, "Efe", 12, 0, out=True)
    _clock(crew, "Efe", 13, 0)
    _clock(crew, "Efe", 17, 0, out=True)
    d = _day(crew, "Efe", MONDAY)
    assert d.clock_in == lagos(MONDAY, 8, 0) and d.clock_out == lagos(MONDAY, 17, 0)


# ------------------------------------------------------------------ overtime


def test_overtime_counts_only_once_the_manager_approves(crew: Crew) -> None:
    db = crew.hr.db
    _clock(crew, "Efe", 8, 0)
    _clock(crew, "Efe", 18, 20, out=True)
    d = _day(crew, "Efe", MONDAY)
    assert (d.overtime_minutes, d.overtime_status) == (80, "pending")
    summary, _ = service.summary(db, crew.hr.actor("Chi"), 2026, 11, q="Efe")
    assert (summary[0].overtime_approved, summary[0].overtime_pending) == (0, 80)

    assert service.overtime_waiting(db, crew.hr.actor("Efe")) == []
    with pytest.raises(NotFound):
        service.decide_overtime(db, crew.hr.actor("Efe"), d.overtime_id, approve=True)  # type: ignore[arg-type]
    waiting = service.overtime_waiting(db, crew.hr.actor("Gbenga"))
    assert [o.minutes for o in waiting] == [80]
    with pytest.raises(ValidationFailed):
        service.decide_overtime(db, crew.hr.actor("Gbenga"), waiting[0].id, approve=False)
    service.decide_overtime(db, crew.hr.actor("Gbenga"), waiting[0].id, approve=True)
    summary, _ = service.summary(db, crew.hr.actor("Chi"), 2026, 11, q="Efe")
    assert (summary[0].overtime_approved, summary[0].overtime_pending) == (80, 0)
    with pytest.raises(Conflict):
        service.decide_overtime(db, crew.hr.actor("Bola"), waiting[0].id, approve=True)


def test_nobody_approves_their_own_overtime(crew: Crew) -> None:
    db = crew.hr.db
    settings.assign(
        db,
        crew.hr.actor("Chi"),
        crew.hr.employee("Dayo"),
        pattern_id=crew.day_shift.id,
        start=MONDAY,
        end=None,
    )
    _clock(crew, "Dayo", 8, 0)
    _clock(crew, "Dayo", 19, 0, out=True)
    entry = db.scalars(
        select(OvertimeEntry).where(OvertimeEntry.employee_id == crew.hr.employee("Dayo"))
    ).one()
    with pytest.raises(PermissionDenied, match="your own"):
        service.decide_overtime(db, crew.hr.actor("Dayo"), entry.id, approve=True)
    service.decide_overtime(db, crew.hr.actor("Chi"), entry.id, approve=True)


# ------------------------------------------------------------------ where and photos


def test_geofence_refuses_outside_the_radius_or_without_a_location(crew: Crew) -> None:
    db = crew.hr.db
    _geofence(crew)
    crew.set_time(lagos(MONDAY, 7, 55))
    with pytest.raises(ValidationFailed) as exc:
        service.clock_in(db, crew.hr.actor("Efe"), position=None)
    assert "Allow location" in exc.value.field_errors["location"]
    with pytest.raises(ValidationFailed) as exc:
        service.clock_in(db, crew.hr.actor("Efe"), position=ACROSS_TOWN)
    assert "1.0 km" in exc.value.field_errors["location"]
    service.clock_in(db, crew.hr.actor("Efe"), position=AT_OFFICE)
    assert _day(crew, "Efe", MONDAY).in_distance_m == 22
    # Clocking out isn't fenced: the location is only recorded.
    crew.set_time(lagos(MONDAY, 17, 0))
    service.clock_out(db, crew.hr.actor("Efe"), position=ACROSS_TOWN)


def test_people_at_other_branches_are_not_fenced(crew: Crew) -> None:
    _geofence(crew)
    settings.assign(
        crew.hr.db,
        crew.hr.actor("Chi"),
        crew.hr.employee("Gbenga"),
        pattern_id=crew.day_shift.id,
        start=MONDAY,
        end=None,
    )
    _clock(crew, "Gbenga", 8, 0)  # no branch, no location: allowed


def test_photo_is_required_and_seen_only_by_the_person_and_hr(crew: Crew) -> None:
    db = crew.hr.db
    _geofence(crew, photo=True)
    crew.set_time(lagos(MONDAY, 7, 55))
    with pytest.raises(ValidationFailed) as exc:
        service.clock_in(db, crew.hr.actor("Efe"), position=AT_OFFICE)
    assert "photo" in exc.value.field_errors
    record_id = service.clock_in(
        db, crew.hr.actor("Efe"), position=AT_OFFICE, photo=("me.jpg", "image/jpeg", JPEG)
    )
    record = db.get(AttendanceRecord, record_id)
    assert record is not None and record.photo_taken and record.photo_file_id
    photo = db.get(StoredFile, record.photo_file_id)
    assert photo is not None and photo.retention_until is not None
    assert photo.retention_until == lagos(MONDAY, 7, 55) + timedelta(days=90)
    for who in ("Efe", "Dayo", "Chi"):  # allowed; the file is still being scanned
        with pytest.raises(Conflict, match="viruses"):
            files.open_for_download(db, crew.hr.actor(who), record.photo_file_id)
    for who in ("Gbenga", "Bola", "Femi"):
        with pytest.raises(NotFound):
            files.open_for_download(db, crew.hr.actor(who), record.photo_file_id)
    assert _day(crew, "Efe", MONDAY).photo_taken


# ------------------------------------------------------------------ corrections


def test_missing_clock_out_counts_no_hours_until_corrected(crew: Crew) -> None:
    db = crew.hr.db
    _clock(crew, "Efe", 8, 0)
    crew.set_time(lagos(MONDAY, 20, 59))
    assert _day(crew, "Efe", MONDAY).status == "working"
    crew.set_time(lagos(MONDAY, 21, 1))  # four hours after the shift ended
    d = _day(crew, "Efe", MONDAY)
    assert (d.status, d.worked_minutes) == ("missing_clock_out", 0)
    with pytest.raises(Conflict, match="not clocked in"):
        service.clock_out(db, crew.hr.actor("Efe"), position=None)

    _clock(crew, "Efe", 8, 0, day=TUESDAY)  # a new day starts normally
    service.request_correction(
        db,
        crew.hr.actor("Efe"),
        crew.hr.employee("Efe"),
        day=MONDAY,
        clock_in=lagos(MONDAY, 8, 0).time(),
        clock_out=lagos(MONDAY, 17, 30).time(),
        reason="Forgot to clock out",
    )
    assert _day(crew, "Efe", MONDAY).pending_correction
    waiting = service.corrections_waiting(db, crew.hr.actor("Gbenga"))
    assert [c.work_date for c in waiting] == [MONDAY]
    assert waiting[0].recorded_out is None
    service.decide_correction(db, crew.hr.actor("Gbenga"), waiting[0].id, approve=True)
    d = _day(crew, "Efe", MONDAY)
    assert (d.status, d.worked_minutes, d.corrected) == ("done", 8 * 60 + 30, True)
    assert (d.overtime_minutes, d.overtime_status) == (30, "pending")


def test_a_correction_can_add_a_day_never_clocked(crew: Crew) -> None:
    db = crew.hr.db
    crew.set_time(lagos(TUESDAY, 9, 0))
    assert _day(crew, "Efe", MONDAY).status == "absent"
    correction = service.request_correction(
        db,
        crew.hr.actor("Dayo"),  # HR recording it for Efe
        crew.hr.employee("Efe"),
        day=MONDAY,
        clock_in=lagos(MONDAY, 8, 0).time(),
        clock_out=lagos(MONDAY, 17, 0).time(),
        reason="Phone was broken",
    )
    service.decide_correction(db, crew.hr.actor("Bola"), correction, approve=True)
    d = _day(crew, "Efe", MONDAY)
    assert (d.status, d.shift_name, d.worked_minutes) == ("done", "Day shift", 480)


def test_correction_rules(crew: Crew) -> None:
    db = crew.hr.db
    crew.set_time(lagos(TUESDAY, 9, 0))
    efe, me = crew.hr.employee("Efe"), crew.hr.actor("Efe")
    eight, five = lagos(MONDAY, 8, 0).time(), lagos(MONDAY, 17, 0).time()
    with pytest.raises(ValidationFailed) as exc:
        service.request_correction(
            db,
            me,
            efe,
            day=TUESDAY + timedelta(days=1),
            clock_in=eight,
            clock_out=five,
            reason="Later",
        )
    assert "work_date" in exc.value.field_errors
    with pytest.raises(ValidationFailed) as exc:
        service.request_correction(
            db,
            me,
            efe,
            day=TUESDAY,
            clock_in=eight,
            clock_out=lagos(TUESDAY, 10, 0).time(),
            reason="Not yet over",
        )
    assert "clock_out" in exc.value.field_errors
    with pytest.raises(ValidationFailed) as exc:
        service.request_correction(
            db, me, efe, day=MONDAY, clock_in=eight, clock_out=five, reason=""
        )
    assert "reason" in exc.value.field_errors
    with pytest.raises(PermissionDenied):
        service.request_correction(
            db,
            me,
            crew.hr.employee("Femi"),
            day=MONDAY,
            clock_in=eight,
            clock_out=five,
            reason="For Femi",
        )
    first = service.request_correction(
        db, me, efe, day=MONDAY, clock_in=eight, clock_out=five, reason="Forgot"
    )
    with pytest.raises(Conflict, match="already"):
        service.request_correction(
            db, me, efe, day=MONDAY, clock_in=eight, clock_out=five, reason="Again"
        )
    with pytest.raises(NotFound):
        service.decide_correction(db, crew.hr.actor("Femi"), first, approve=True)
    with pytest.raises(ValidationFailed):
        service.decide_correction(db, crew.hr.actor("Gbenga"), first, approve=False, note="no")
    service.decide_correction(
        db, crew.hr.actor("Gbenga"), first, approve=False, note="You were on site visit"
    )
    assert _day(crew, "Efe", MONDAY).status == "absent"
    views = service.corrections_for(db, me, efe, MONDAY, MONDAY)
    assert views[0].status == "rejected" and views[0].decision_note == "You were on site visit"
    second = service.request_correction(
        db, me, efe, day=MONDAY, clock_in=eight, clock_out=five, reason="Forgot"
    )
    service.cancel_correction(db, me, second)
    assert service.corrections_waiting(db, crew.hr.actor("Gbenga")) == []


# ------------------------------------------------------------------ schedules


def test_night_shift_belongs_to_the_day_it_started(crew: Crew) -> None:
    db = crew.hr.db
    night = settings.save_pattern(
        db,
        crew.hr.actor("Chi"),
        None,
        name="Night shift",
        start_time="20:00",
        end_time="06:00",
        break_minutes="30",
        weekdays=["0", "1", "2", "3", "4"],
        late_grace="15",
        early_grace="15",
        is_active=True,
    )
    settings.set_roster_day(
        db, crew.hr.actor("Chi"), crew.hr.employee("Femi"), day=MONDAY, choice=str(night)
    )
    _clock(crew, "Femi", 20, 10)
    _clock(crew, "Femi", 0, 30, day=TUESDAY, out=True)  # a break, then back in after midnight
    _clock(crew, "Femi", 1, 0, day=TUESDAY)
    _clock(crew, "Femi", 6, 40, day=TUESDAY, out=True)
    d = _day(crew, "Femi", MONDAY)
    assert (d.status, d.shift_name, d.late_minutes) == ("done", "Night shift", 0)
    assert d.worked_minutes == 10 * 60 + 30 - 30
    assert d.overtime_minutes == 40
    assert _day(crew, "Femi", TUESDAY).clock_in is None


def test_roster_day_off_replaces_the_usual_shift(crew: Crew) -> None:
    db, chi, efe = crew.hr.db, crew.hr.actor("Chi"), crew.hr.employee("Efe")
    crew.set_time(lagos(TUESDAY, 9, 0))
    assert _day(crew, "Efe", MONDAY).status == "absent"
    settings.set_roster_day(db, chi, efe, day=MONDAY, choice="off", note="Swapped with Femi")
    assert _day(crew, "Efe", MONDAY).status == "off"
    week = settings.roster_week(db, chi, MONDAY)
    efe_row = next(r for r in week if r.employee_id == efe)
    assert [c.kind for c in efe_row.cells] == [
        "off",
        "usual",
        "usual",
        "usual",
        "usual",
        "none",
        "none",
    ]
    settings.set_roster_day(db, chi, efe, day=MONDAY, choice="usual")
    assert _day(crew, "Efe", MONDAY).status == "absent"
    with pytest.raises(PermissionDenied):
        settings.set_roster_day(db, crew.hr.actor("Efe"), efe, day=MONDAY, choice="off")


def test_a_new_usual_shift_ends_the_previous_one(crew: Crew) -> None:
    db, chi, efe = crew.hr.db, crew.hr.actor("Chi"), crew.hr.employee("Efe")
    late = settings.save_pattern(
        db,
        chi,
        None,
        name="Late shift",
        start_time="12:00",
        end_time="21:00",
        break_minutes="60",
        weekdays=["0", "1", "2", "3", "4", "5"],
        late_grace="15",
        early_grace="15",
        is_active=True,
    )
    settings.assign(db, chi, efe, pattern_id=late, start=date(2026, 12, 1), end=None)
    rows = [a for a in settings.assignments(db, chi) if a.employee_id == efe]
    assert [(a.pattern.split()[0], a.end_date) for a in rows] == [
        ("Day", date(2026, 11, 30)),
        ("Late", None),
    ]
    with pytest.raises(Conflict):
        settings.assign(db, chi, efe, pattern_id=late, start=date(2026, 11, 20), end=None)
    saturday = date(2026, 12, 5)
    days = service.days_for(db, [efe], saturday, saturday)[efe]
    assert days[0].shift_name == "Late shift"


def test_only_people_with_a_shift_are_tracked(crew: Crew) -> None:
    db = crew.hr.db
    crew.set_time(lagos(MONDAY, 10, 0))
    _clock(crew, "Hauwa", 9, 0)  # no shift, but anyone may clock in
    crew.set_time(lagos(MONDAY, 10, 0))
    team = {p.name: p.day for p in service.team_today(db, crew.hr.actor("Chi"))}
    assert team["Efe Test"].status == "not_in"
    assert team["Hauwa Test"].status == "working" and not team["Hauwa Test"].scheduled
    assert "Ada Test" not in team  # no shift, no clock-in
    crew.set_time(lagos(MONDAY, 18, 0))
    _clock(crew, "Hauwa", 18, 0, out=True)
    d = _day(crew, "Hauwa", MONDAY)
    assert (d.late_minutes, d.overtime_minutes, d.worked_minutes) == (0, 0, 9 * 60)


def test_leave_and_holidays_are_not_absences(crew: Crew) -> None:
    db = crew.hr.db
    wednesday, thursday = MONDAY + timedelta(days=2), MONDAY + timedelta(days=3)
    db.add(
        LeaveRequest(
            employee_id=crew.hr.employee("Efe"),
            leave_type_id=leave_type(db, "annual").id,
            start_date=wednesday,
            end_date=thursday,
            end_half=True,
            days=Decimal("1.5"),
            year=2026,
            status="approved",
        )
    )
    db.flush()
    crew.set_time(lagos(thursday, 13, 30))
    _clock(crew, "Efe", 13, 30, day=thursday)  # back from half a day's leave: not late
    crew.set_time(lagos(MONDAY + timedelta(days=4), 18, 0))
    assert _day(crew, "Efe", wednesday).status == "on_leave"
    thu = _day(crew, "Efe", thursday)
    assert thu.leave_part == "morning" and thu.late_minutes == 0
    summary, _ = service.summary(db, crew.hr.actor("Chi"), 2026, 11, q="Efe")
    assert summary[0].leave_days == Decimal("1.5")
    # Christmas is a seeded public holiday.
    crew.set_time(lagos(date(2026, 12, 28), 9, 0))
    assert _day(crew, "Efe", date(2026, 12, 25)).status == "holiday"


def test_monthly_summary_counts_the_month(crew: Crew) -> None:
    db = crew.hr.db
    _clock(crew, "Efe", 8, 20)  # late
    _clock(crew, "Efe", 17, 0, out=True)
    _clock(crew, "Efe", 8, 0, day=TUESDAY)  # missing its clock-out
    crew.set_time(lagos(MONDAY + timedelta(days=2), 9, 0))
    row = next(
        r for r in service.summary(db, crew.hr.actor("Chi"), 2026, 11)[0] if r.name == "Efe Test"
    )
    # Absent on the ten working days from 2 Nov; late on the 16th; no clock-out on the 17th.
    assert (row.present, row.late, row.missing_clock_out) == (2, 1, 1)
    assert row.absent == 10
    assert row.scheduled == 21
    assert row.worked_minutes == 8 * 60 + 40 - 60
    # Managers see their team; the employee only themself.
    assert {r.name for r in service.summary(db, crew.hr.actor("Efe"), 2026, 11)[0]} == {"Efe Test"}


# ------------------------------------------------------------------ settings


def test_settings_are_validated(crew: Crew) -> None:
    db, chi = crew.hr.db, crew.hr.actor("Chi")
    pattern = {
        "name": "Weekend",
        "start_time": "09:00",
        "end_time": "15:00",
        "break_minutes": "30",
        "weekdays": [],
        "late_grace": "15",
        "early_grace": "15",
        "is_active": True,
    }
    with pytest.raises(ValidationFailed) as exc:
        settings.save_pattern(db, chi, None, **pattern)  # type: ignore[arg-type]
    assert "weekdays" in exc.value.field_errors
    with pytest.raises(ValidationFailed) as exc:
        settings.save_pattern(db, chi, None, **{**pattern, "weekdays": ["5"], "name": "day SHIFT"})  # type: ignore[arg-type]
    assert "name" in exc.value.field_errors
    with pytest.raises(ValidationFailed) as exc:
        settings.save_pattern(db, chi, None, **{**pattern, "weekdays": ["5"], "late_grace": "300"})  # type: ignore[arg-type]
    assert "late_grace" in exc.value.field_errors
    with pytest.raises(PermissionDenied):
        settings.save_pattern(db, crew.hr.actor("Dayo"), None, **{**pattern, "weekdays": ["5"]})  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed) as exc:
        settings.save_location(
            db,
            chi,
            crew.branch_id,
            geofence_enabled=True,
            latitude="",
            longitude="",  # type: ignore[arg-type]
            radius_m="150",
            require_photo=False,
        )
    assert "latitude" in exc.value.field_errors
    with pytest.raises(ValidationFailed) as exc:
        settings.save_location(
            db,
            chi,
            crew.branch_id,
            geofence_enabled=False,
            latitude="95",
            longitude="3",  # type: ignore[arg-type]
            radius_m="150",
            require_photo=False,
        )
    assert "latitude" in exc.value.field_errors
    assert db.scalars(select(ShiftPattern).where(ShiftPattern.name == "Weekend")).first() is None
    assert (
        service.distance_m(
            Decimal("6.4281"), Decimal("3.4219"), Decimal("6.4371"), Decimal("3.4219")
        )
        == 1001
    )


def test_clocking_in_after_the_shift_ended_can_still_clock_out(crew: Crew) -> None:
    _clock(crew, "Efe", 22, 0)  # extra work late in the evening
    crew.set_time(lagos(TUESDAY, 1, 30))
    assert _day(crew, "Efe", MONDAY).status == "working"
    _clock(crew, "Efe", 1, 45, day=TUESDAY, out=True)
    d = _day(crew, "Efe", MONDAY)
    assert (d.status, d.late_minutes, d.overtime_minutes) == ("done", 14 * 60, 8 * 60 + 45)
