# Organisation structure

Phase 4. Decisions are in ADR 0015; permissions in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `branches` | Code (unique), name, address, state, active flag. |
| `cost_centres` | Code (unique), name, active flag. |
| `grades` | Code (unique), name, rank (higher is more senior), active flag. |
| `departments` | Code (unique), name, optional parent department, optional cost centre, active flag. |
| `positions` | Title, department, optional parent position, optional grade and branch, active flag. |
| `position_assignments` | Position, employee, kind (`primary`, `acting`, `interim`), start date, optional inclusive end date, reason, who made it, when it was ended early. |
| `org_state` | A single `version` counter, incremented by every structural change. |
| `employees` (HR module) | First name, last name, other names, work email, optional sign-in account, status (`active`, `on_leave`, `suspended`, `terminated`). |

## Rules

- A position has one holder of each kind at a time; an employee has one primary position
  at a time. Assignment dates are calendar dates in the organisation's timezone.
- The holder of a position is the primary holder, else the acting holder, else the
  interim holder; with none, the position is vacant.
- Departments and positions can't be moved under their own descendants.
- A department or position with active children or current holders can't be deactivated.
- Inactive positions keep their history and are skipped by the resolver.

## Resolver

All functions take an "as of" date (default: today in the organisation's timezone).

| Function | Answers |
|---|---|
| `holder(position)` | Who holds this position. |
| `manager_of(employee)` | The first holder above the employee's position, skipping vacancies. |
| `reports_of(employee, direct)` | Who reports to the employee, directly or at any depth. |
| `approval_chain(employee)` | The managers above, in order, without repeats. |
| `departments_of(employee)` | Departments of the positions the employee holds. |
| `departments_under(department)` | A department and all its sub-departments. |

## Screens

HR → Org chart, Position detail, Departments, People, Structure setup (branches, cost
centres, grades). Administration → Organisation (legal details, regional settings,
document colours).
