# 0018. Leave: types, entitlements, working days, approval and balances

- Status: Accepted
- Date: 2026-11-19

## Context

Brief section 9.4 and Phase 7. Phase 7 is delivered in two gates: leave (7a, this ADR)
and attendance (7b) (confirmed by the product owner on 2026-11-19).

## Decision

**Module.** `app/modules/leave`, depending on HR (employees) and the org resolver.

**Leave year.** The calendar year, 1 January to 31 December (confirmed 2026-11-19).

**Leave types** are editable rows: name, paid, how days are counted (working days, or
calendar days for maternity), whether the type has a balance (unpaid leave doesn't),
accrual (yearly or monthly), carry-over cap and expiry month, whether HR must give a
final approval, and the length above which a medical certificate is required.

**Entitlements** are rules per type: an optional grade, a minimum length of service in
months, and days per year. For a person and year, the most specific matching rule wins
(their grade before "any grade", then the longest service requirement they meet).
Yearly accrual gives the whole amount on 1 January; in the year someone first becomes
eligible it is pro-rated by the whole months left (confirm). Monthly accrual gives a
twelfth for each month started. Defaults (confirm): annual 6 working days after 12
months' service, sick 12 working days with a certificate over 2 days, maternity 12
weeks (84 calendar days). Paternity, compassionate and study start at zero until HR sets
them; unpaid has no balance (confirmed 2026-11-19).

**Working days** skip Saturdays, Sundays and public holidays. Public holidays are an
admin-managed list; fixed-date and Easter holidays are seeded, Islamic holidays are added
when announced. A request may start in the afternoon or end at midday (half days,
confirmed 2026-11-19).

**Balance** for a person, type and year = entitlement + unexpired carry-over +
adjustments − approved − pending. A request that needs more than the available balance
is refused (confirmed 2026-11-19). HR adjustments are audited rows with a reason.
Carry-over is written at the start of each year by a job, up to the type's cap, expiring
at the end of the configured month.

**Approval.** At submission the request records the employee's manager from the org
resolver. Step one is the manager step: anyone whose `leave.request.approve` grant covers
the employee may decide it (the manager, managers above, delegates, HR); step two, when
the type requires it, is HR (`leave.request.approve_hr`). Nobody decides their own
request (SoD rule). Someone without a manager goes straight to HR. Balances count a
request as pending until it is approved; rejecting or cancelling restores the days.
Cancelling an approved request is allowed until it starts; afterwards HR cancels it.

**Certificates** are files with purpose `leave_certificate` (class `sensitive`): the
employee attaches them; HR and the employee can open them; the manager sees only that one
is attached.

## Consequences

Attendance (7b) and payroll (Phase 9) read approved unpaid leave days through the leave
service.
