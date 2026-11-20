# 0019. Attendance: shifts, clocking, lateness, overtime and corrections

- Status: Accepted
- Date: 2026-11-19

## Context

Brief section 9.5 and Phase 7 (second gate, 7b). Payroll (Phase 9) needs a monthly
summary it can use for deductions and overtime.

## Decision

**Module.** `app/modules/attendance`, using HR (employees, branches), leave (approved
leave and public holidays, through `leave/service.py`) and the org resolver.

**Shifts.** A shift pattern has a start and end time (an end before the start crosses
midnight), a break, working weekdays, and late and early grace (15 minutes each by
default, confirm). People are given a pattern for a date range; a roster entry replaces
the pattern for one day (another pattern, or a day off). Only people with a shift on a
day are judged for lateness and absence (confirmed 2026-11-19); anyone may clock in.

**Clocking.** One record per person per work day (the local date of clock-in): the first
clock-in and the last clock-out. If the person's branch has a geofence, the browser's
location is required and must be within the radius; otherwise clocking is refused
(confirmed 2026-11-19). A branch can require a photo; photos are files with purpose
`attendance_photo`, class `sensitive` (the employee and HR see them), deleted 90 days
after upload (confirm).

**A day's status** is worked out when read, never stored: on leave (approved leave from
the leave module), public holiday, absent (scheduled, the shift has ended, no clock-in),
late (clock-in after start + grace), left early (clock-out before end − grace), missing
clock-out (clocked in, no clock-out four hours after the shift end). Worked time is
clock-out − clock-in − break.

**Overtime** is time past the scheduled shift end. It is recorded at clock-out as
pending and counts only once approved (confirmed 2026-11-19).

**Corrections.** An employee asks to set a day's clock-in and clock-out with a reason; a
forgotten clock-out is fixed this way (confirmed 2026-11-19). Approving applies the
times and records who and why.

**Approvals** (corrections and overtime) follow leave: anyone whose
`attendance.record.approve` grant covers the employee (the manager, managers above,
delegates, HR) may decide; nobody decides their own.

**Monthly summary** per person: scheduled days, present, absent, late, left early,
missing clock-out, leave days, worked hours, approved and pending overtime; exportable
as CSV for payroll.

## Consequences

Payroll reads the summary through `attendance/service.py`. Several clock-ins in a day
are not kept separately (first in, last out).
