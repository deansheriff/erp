# Leave

Phase 7a. Decisions are in ADR 0018; permissions in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `leave_types` | Code, name, paid, counting (`working`, `calendar`), has balance, accrual (`yearly`, `monthly`), carry-over cap and expiry month, HR approval, certificate threshold (days), active. |
| `leave_entitlement_rules` | Type, optional grade, minimum months of service, days per year. |
| `public_holidays` | Date and name. |
| `leave_requests` | Employee, type, start and end dates, half-day flags, days, reason, status (`pending`, `approved`, `rejected`, `cancelled`), current step (`manager`, `hr`), manager at submission, certificate file. |
| `leave_decisions` | Request, step, decision, who, when, note. |
| `leave_adjustments` | Employee, type, year, days (+/−), kind (`adjustment`, `carry_over`), expiry date, reason, who. |

## Defaults (confirm before go-live)

| Type | Counts | Paid | Entitlement | HR approval | Certificate |
|---|---|---|---|---|---|
| Annual | working days | yes | 6 days after 12 months' service | no | — |
| Sick | working days | yes | 12 days | no | over 2 days |
| Maternity | calendar days | yes | 84 days (12 weeks) | yes | — |
| Paternity | working days | yes | 0 until HR sets it | yes | — |
| Compassionate | working days | yes | 0 until HR sets it | no | — |
| Study | working days | yes | 0 until HR sets it | yes | — |
| Unpaid | working days | no | no balance | yes | — |

No carry-over by default. Weekends are Saturday and Sunday.

## Flows

**Request.** Pick type and dates (half days allowed) → days counted (working or calendar)
→ refused if over the available balance → pending with the manager step (or HR when
there is no manager) → certificate required before approval when the type asks for one.

**Approve.** Manager step → HR step if the type requires it → approved. Reject needs a
note. Cancelling before the start date restores the balance.

**Year start.** A job on 1 January writes carry-over (up to each type's cap, expiring at
the end of its expiry month).

## Screens

Leave → My leave, Request leave, Approvals, Team calendar, Balances, Settings.
