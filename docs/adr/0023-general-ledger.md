# 0023. Finance, first gate: the general ledger

- Status: Accepted
- Date: 2026-11-20

## Context

Brief section 9.8 and Phase 10. The phase has five gates (confirmed 2026-11-20): 10a the
general ledger, 10b receivables and tax, 10c payables, 10d banking and reconciliation,
10e fixed assets. This ADR covers 10a. Payroll (ADR 0022) and expenses (ADR 0020) already
queue balanced journals for the ledger.

## Decision

**Module.** `app/modules/finance`. Other modules hand it journals through
`finance.service.post_system_journal`, in the same transaction as the document that
causes them; finance imports no other business module.

**Chart of accounts.** Hierarchical accounts typed asset, liability, equity, income or
expense. Header accounts group others and take no postings. Balance-sheet accounts carry a
cash-flow class (cash, operating, investing or financing) for the cash flow statement. A
Nigerian SME chart is seeded with the codes payroll and expenses already use (confirmed
2026-11-20; codes to confirm). Accounts with postings can't change type or be deleted;
they can be deactivated.

**Currency.** Naira only for now (confirmed 2026-11-20); every journal line carries its
currency (NGN) so foreign currencies can follow without changing the ledger.

**Financial years** run January to December (confirmed 2026-11-20), each with 12 monthly
periods. A Finance Manager opens a year, closes months, and reopens a month with a
reason, which is audited (confirmed 2026-11-20). A month with draft journals dated in it
can't be closed. Once all twelve months are closed, closing the year posts the year's
income and expenses to retained earnings; a closed year and its months can't be reopened.

**Journals.**
- Manual journals are drafted by one person and posted by another (confirmed 2026-11-20).
- A journal posts only if it balances, has at least two lines, uses active non-header
  accounts, and is dated in an open month of an open year.
- Numbers (`JV-00001`) are gap-free and given at posting, in the posting transaction.
- Posted journals are never edited: they are reversed by a new journal with the lines
  swapped, dated in an open month.
- System journals from payroll and expenses post at once (confirmed 2026-11-20). If their
  date is in a closed or missing month, or an account code isn't in the chart, they are
  held with the reason (confirmed 2026-11-20). Finance posts a held journal once its month
  is open, or re-dates it into an open month with a reason.

**Recurring journals** are templates (lines, a day of the month, a first and optional last
month). A daily task drafts each one that falls due; someone other than the template's
author posts it (confirmed 2026-11-20).

**Reports**, from posted journals: trial balance, profit and loss, balance sheet, cash
flow (indirect method, from the cash-flow class of each balance-sheet account), and the
general ledger for an account with opening balance and running balance. The year-end
closing journal is left out of profit and loss and cash flow. Profit and loss can be
filtered by cost centre. Amounts show in accounting format, negatives in parentheses.

## Consequences

The ledger is the single place balances come from; receivables, payables, banking and
fixed assets (10b to 10e) post through the same service. Journals queued by payroll or
expenses before 10a (development data only) stay in their queues.
