# 0020. Expenses: claims, tiered approval, reimbursement and cash advances

- Status: Accepted
- Date: 2026-11-20

## Context

Brief section 9.6 and Phase 8. Finance (the general ledger) arrives in Phase 10 and
projects in Phase 11, so reimbursement can't post to a ledger yet.

## Decision

**Module.** `app/modules/expenses`, using HR (people, cost centres through the org
service), the org resolver (managers) and files (receipts).

**Categories** have a ledger account code (text until Phase 10), an optional limit per
claim and a "receipt needed above" amount (₦0 means always, confirm). Lines below it may
say why there's no receipt.

**Claims and advances** are one document type with a `kind`: a claim has lines; a cash
advance has a purpose and an amount. Numbers are gap-free (`EXP-00001`, `ADV-00001`).
Amounts are naira only for now (confirmed 2026-11-20). Lines are tagged with a cost
centre, defaulting from the person's department; projects come in Phase 11.

**Statuses:** `draft`, `submitted`, `approved`, `rejected`, `returned`, `reimbursed`,
`cancelled`. A returned document goes back to the employee to edit and resubmit; its
approval steps are rebuilt.

**Approval chain** (confirmed 2026-11-20), built at submission from the org chart: the
line manager approves every document; above the second-level amount (₦100,000, confirm)
the next manager up approves too; above the Finance amount (₦500,000, confirm) Finance
approves last. A step is decided by its named approver, someone that approver has
delegated `expenses.claim.approve` to, or anyone holding `expenses.claim.approve` across
the organisation (Finance or HR stepping in). Without a manager, the step goes to those
organisation-wide approvers. Nobody approves their own claim. Approvers may correct line
amounts; the original stays on the line and the change is audited.

**Limits** (confirmed 2026-11-20): over a category's per-claim limit, or over the
person's weekly or monthly spending limit, a claim can still be submitted; approvers see
a clear flag. Organisation-wide weekly and monthly limits can be overridden per person.

**Reimbursement** (confirmed 2026-11-20): someone with `expenses.claim.reimburse` marks
approved documents paid with a date, method and reference, several at once. They can't
pay their own claim, nor one they approved unless they hold
`expenses.claim.reimburse_override`.

**Cash advances** (confirmed 2026-11-20): a paid advance can be named on later claims; at
payment the claim uses up what is left of the advance first. Whatever is left over is
owed by the employee; Finance records repayments; the advance is settled when nothing
is left. Payroll deductions can come with Phase 9.

**Ledger** (confirmed 2026-11-20): every payment or repayment queues a balanced journal
(expense accounts by category and cost centre, the advances account, the bank account),
`queued` until Phase 10 posts it.

## Consequences

Phase 10 reads queued journals through `expenses/service.py`. Approval authority by
amount per approver is not modelled; amounts tier the chain instead.
