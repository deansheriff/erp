# Finance

Phase 10, in five gates. 10a, the general ledger, is in ADR 0023; permissions are in
`docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `finance_accounts` | The chart of accounts: code, name, type, parent header, cash-flow class. |
| `fiscal_years`, `fiscal_periods` | Financial years (January to December) and their twelve months, open or closed. |
| `journals`, `journal_lines` | Drafted, held and posted journals and their lines (account, cost centre, debit, credit, currency). |
| `recurring_journals`, `recurring_journal_lines` | Templates drafted on their day each month. |
| `finance_settings` | The retained earnings account (one row). |

## How journals get to the ledger

- **Manual:** drafted by one person (`finance.journal.create`), posted by another
  (`finance.journal.post`). Drafts can be saved out of balance; posting needs at least two
  lines that balance, active accounts that take postings, and an open month.
- **From other modules:** payroll (when a run is posted or paid, or a loan approved) and
  expenses (when a claim is paid or an advance repaid) hand their journal to
  `finance.service.post_system_journal` in the same transaction. It posts at once, or is
  held with the reason (closed or unopened month, account not in the chart). Finance posts
  a held journal once its month is open, or re-dates it into an open month with a reason.
- **Recurring:** a daily task (01:30 UTC) drafts each template that falls due, carrying the
  template's author, so someone else posts it. Opening the Journals page also drafts any
  that are due.
- **Reversal:** posted journals are never edited; a reversal posts the same lines the
  other way round.
- Numbers (`JV-00001`) are gap-free and given at posting.

## Periods

A Finance Manager opens a year (twelve open months), closes months (not while drafts are
dated in them) and reopens one with a reason. Once every month is closed, closing the year
posts its income and expenses to retained earnings (3200 by default) and can't be undone.

## Defaults (confirm before go-live)

A Nigerian SME chart, headed 1 Assets, 2 Liabilities, 3 Equity, 4 Income, 5 Cost of
sales and 6 Operating expenses, using the codes payroll and expenses already use (1010
bank, 1410 staff cash advances, 1420 staff loans, 2110 to 2170 payroll liabilities, 6000
to 6020 staff costs, 6100 to 6900 expense categories). Retained earnings: 3200.

## Reports

Trial balance and balance sheet as at a date; profit and loss (by cost centre if wanted),
cash flow (indirect, by each balance-sheet account's cash-flow class; unclassified ones
count as operating) and the general ledger for an account, over a date range. The
year-end closing journal is left out of profit and loss and cash flow. Trial balance and
general ledger download as CSV.

## Screens

Finance → Journals (and each journal), Chart of accounts, Recurring, Periods, Reports.
