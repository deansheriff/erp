# Attendance and shifts

Phase 7b. Decisions are in ADR 0019; permissions in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `shift_patterns` | Name, start and end time, break minutes, working weekdays, late and early grace (minutes), active. |
| `shift_assignments` | Employee, pattern, from and (optional) to date. One at a time per person. |
| `roster_entries` | Employee, date, pattern or day off: replaces the assignment for that day. |
| `attendance_locations` | Branch, geofence on/off, latitude, longitude, radius in metres, photo required. |
| `attendance_records` | Employee, work date, clock-in and clock-out times, locations and distances, photo, scheduled shift snapshot. |
| `attendance_corrections` | Employee, work date, requested in and out, reason, status (`pending`, `approved`, `rejected`, `cancelled`), decider. |
| `overtime_entries` | Record, minutes, status (`pending`, `approved`, `rejected`), decider. |

## Defaults (confirm before go-live)

- Grace: 15 minutes late, 15 minutes early, per pattern.
- Missing clock-out: flagged four hours after the shift ends.
- Attendance photos deleted after 90 days.

## Day statuses

`on_leave`, `holiday`, `off` (not scheduled), `absent`, `present` with flags `late`,
`left_early`, `missing_clock_out`, `outside_shift` (clocked in with no shift).

## Screens

Attendance → Clock in, Timesheet, Team today (with corrections and overtime to approve),
Monthly summary (CSV export), Settings (patterns, branch locations, assignments, roster).
