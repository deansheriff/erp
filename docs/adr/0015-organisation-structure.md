# 0015. Organisation structure: positions, assignments and the org resolver

- Status: Accepted
- Date: 2026-11-18

## Context

Brief sections 4.1 (team and department scopes), 9.1 and Phase 4. Reporting lines must
come from a **position hierarchy**, never from a `manager_id` on the employee. Approval
routing (later phases) and the `team` and `department` scopes all depend on it.

## Decision

**Where things live.**
- `app/org` owns the structure: branches, cost centres, grades, departments (nestable),
  positions (a tree) and position assignments. It knows people only by employee ID.
- `app/modules/hr` owns the `employees` table. Phase 4 adds only what the chart needs
  (names, work email, optional sign-in account, status). Phase 6 adds the HR record to
  the same module. Core packages never import `app.modules` (import-linter contract).
- `app/modules/hr/org_access.py` adapts the org resolver to the policy engine's
  `OrgResolver` protocol (user → employee → positions). The app wires it at start-up.

**One holder per position.** A position is one seat. Five sales executives are five
positions with the same title. Confirmed by the product owner on 2026-11-18.

**Assignment kinds.** An assignment links an employee to a position as `primary`,
`acting` or `interim`, with a start date and an optional (inclusive) end date. At any
date a position has at most one assignment of each kind, and an employee has at most
one primary position.

**Who holds a position on a date.** The primary holder if there is one; otherwise the
acting holder; otherwise the interim holder; otherwise the position is vacant. A primary
holder always wins, even when an acting holder also exists (confirmed 2026-11-18).
Absences are covered by delegations (ADR 0013), not by acting assignments.

**Resolver** (`app/org/resolver.py`), always "as of" a date (default today):
- `holder(position)`: as above.
- `manager_of(employee)`: start from the employee's primary position (or, without one,
  their acting then interim position), walk up the parents, skip vacant positions and
  positions the employee holds, and return the first holder.
- `reports_of(employee, direct=False)`: everyone below the positions the employee holds
  (as the effective holder). Direct reports are the first occupied positions under them;
  vacant positions are looked through.
- `approval_chain(employee)`: managers in order up to the top, without repeats.
- `departments_of(employee)` and `departments_under(department)`.

A snapshot of the structure for one date is cached in-process under an `org_version`
counter that every structural change increments, so workers never serve a stale chart.

**Cycles.** Moving a department or position under one of its own descendants is refused
when saving. The resolver also guards every walk with a visited set: if a cycle exists
anyway (bad data), it stops, logs `org_cycle_detected` and treats the rest as vacant.

**Scopes.** `team` = the actor's reports (all levels) through positions they currently
hold as effective holder, acting included. `department` = the departments of those
positions and every department under them.

**Organisation details** gain brand colours for documents. The logo waits for the files
service (Phase 5).

## Consequences

Approval routing in later phases calls `approval_chain` and never walks positions itself.
Changing the chart changes who can see what immediately (the next request), with no
cache to clear.
