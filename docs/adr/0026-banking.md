# 0026: Banking

Status: proposed (Phase 10d), decisions confirmed 2026-11-22

## Context

The brief asks for bank accounts, statement import (CSV/Excel, plus OFX if simple),
auto-matching rules, manual reconciliation with a difference that must reach ₦0.00, and
transfers between accounts. Tax returns marked paid in 10b posted nothing; their payments
belong here.

## Decisions

**Bank accounts.** Each bank account has its own ledger account under 10 Cash and bank:
adding one creates the next free code (1011, 1012 …). The seeded 1010 becomes the first
bank account, with its bank details to fill in. A bank account keeps:

- its bank details: bank, CBN code, NUBAN account number and account name;
- its statement layout: the date, description and reference columns; a signed amount
  column, or money-in and money-out columns; the date format; and rows to skip. A column
  is named by its header or by its number.

**Statement import.** CSV and Excel (.xlsx) are read with the account's layout, and OFX
(SGML or XML) is read directly. The uploaded file is kept through the files service
(purpose `bank_statement`, class `banking`, so downloads are audited) for six years
**(confirm)**. Its lines are parsed from the bytes at upload. The file detector now
recognises plain text, for CSV and OFX; no other purpose accepts it.

Each line gets a fingerprint (date, amount, description, reference, and which repeat of
those it is within the file). Re-importing an overlapping statement skips lines already
imported.

**Matching.** A statement line matches one posted journal line on the account's ledger
account with the same amount (money in is a debit). A journal line is matched at most
once.

1. **On import**, a line with exactly one candidate within 3 days (editable, to confirm)
   is matched automatically.
2. **Rules** handle the rest. Each rule is "the description contains …", money in, out
   or either, for one bank account or all, against a ledger account and optional cost
   centre. The first active rule in order turns a line into an **entry**.
3. **By hand**, someone can match a line to any candidate with the same amount, unmatch
   it, or give it an entry.

**Reconciliation** of a bank account to a statement date and closing balance:

- difference = statement balance − (ledger balance at the date − ledger entries not yet
  matched to the statement + entries still to post);
- every statement line up to the date must be matched or have an entry;
- one person submits it once the difference is ₦0.00, and someone else approves it (SoD
  rule `finance.bank_four_eyes`).

Approval posts each entry's journal on its line's date, then locks every line up to the
date. Locked lines can't be unmatched. Each reconciliation must be dated after the last
approved one, and there's one open reconciliation per account at a time.

**Transfers** (`TRF-`) between two of the organisation's bank accounts are recorded by one
person and approved by another (the same SoD rule). Approval posts
debit the receiving account and credit the paying one.

**Tax payments.** Marking a VAT, WHT or stamp duty return paid now asks for the bank
account and posts the payment with source `tax`, dated the payment date:

- **WHT and stamp duty:** debit the liability with the filed amount, credit the bank.
- **VAT:** debit VAT output with the filed output, credit VAT input with the filed input,
  and credit the bank with the net. When input is more than output, nothing is paid. Input
  is cleared only up to output, and the rest stays in VAT input.

Journals from banking post with source `bank` (rule entries and transfers) and `tax`. Like
receivables and payables, they're corrected with a manual journal, never reversed from the
Journals screen.

## Consequences

- Statement balances are entered by hand on the reconciliation; files are not trusted for
  them.
- Excess input VAT carried forward isn't added to the next month's return. The returns
  read each month's own movements (ADR 0024).
- Bank feeds (direct connections) aren't covered.
