# Expenses and cash advances

Phase 8. Decisions are in ADR 0020; permissions in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `expense_categories` | Name, ledger account code, limit per claim, receipt needed above, active. |
| `expense_settings` | One row: second-level and Finance amounts, weekly and monthly limits, advances and bank account codes. |
| `expense_person_limits` | Weekly and monthly limits for one person, replacing the defaults. |
| `expense_documents` | Claims and advances: number, kind, person, title or purpose, status, totals, advance used, payment. |
| `expense_lines` | A claim's lines: date, category, description, cost centre, amount (and the original if an approver changed it), receipt or reason for none. |
| `expense_steps` | The approval chain of a submission: order, kind (`manager`, `second`, `finance`), named approver, decision. |
| `expense_repayments` | Money an employee pays back on an advance. |
| `expense_journals`, `expense_journal_lines` | Balanced journals queued for the ledger (Phase 10). |

## Projects

A claim line can name an active project (ADR 0031). When the claim is reimbursed, the
project goes with the line into the journal, and the project's Expenses figure picks it
up.

## Defaults (confirm before go-live)

- Second-level approval above ₦100,000; Finance approval above ₦500,000.
- Weekly limit ₦50,000 and monthly limit ₦150,000 per person (warn only).
- Receipt needed above ₦0 (always) for each seeded category.
- Categories: Travel, Accommodation, Meals, Fuel, Training, Office supplies, Other.
- Ledger codes are placeholders until the chart of accounts exists (Phase 10).

## Screens

Expenses → My expenses, claim and advance forms, Approvals, Payments, Settings.
