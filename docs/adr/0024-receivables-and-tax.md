# 0024: Receivables and tax

Status: proposed (Phase 10b), decisions confirmed 2026-11-22

## Context

The brief asks for customers; quotes, sales orders and invoices; receipts allocated to
invoices; credit notes; aging, statements and reminders; VAT on invoices; configurable
withholding-tax (WHT) rates by transaction type; stamp duty; and tax returns with a
filing status. Every posted document creates its journal in the same transaction, posted
documents are only reversed, and numbering is gap-free.

## Decisions

**Documents.** Quotes, sales orders, invoices and credit notes share one table
(`sales_documents`, with a `kind`) and one line table. Every step is optional: an invoice
can be raised directly, and a quote or order can be converted, which copies its lines into
a new draft and marks the source converted (restored if that draft is deleted). Numbers
are given when a document leaves draft: `QT-`, `SO-`, `INV-`, `CN-`; receipts are `RCT-`
and customers `C-`.

- Quotes: draft → sent → accepted or declined, or converted.
- Orders: draft → confirmed → invoiced, or cancelled.
- Invoices: draft → issued. Part paid, paid and overdue are worked out from allocations
  and the due date, never stored.
- Credit notes: drafted by one person and issued by another (SoD rule
  `finance.credit_note_four_eyes`). Whoever drafts an invoice may issue it.

**VAT.** Prices exclude VAT. Each line is standard, zero-rated or exempt. The standard rate
is effective-dated configuration, seeded at 7.5% from 1 February 2020 **(confirm)**. The
rate in force on the document date is stored on each line when it is issued, so later rate
changes never alter an issued invoice. VAT is rounded per line to the kobo.

**Stamp duty.** An invoice can carry one stamp duty type, configured as a flat amount or a
percentage of the net amount. Finance adds the types; none are seeded. The duty is added to
the invoice total and credited to stamp duty payable.

**Withholding tax.** WHT types are seeded (supply of goods, contracts and agency,
consultancy and professional, management and technical, rent, commission, dividends,
interest, royalties, directors' fees) with **no rates**. Finance enters a company and an
individual rate before using them. A receipt records cash received plus WHT the customer
deducted; both settle the invoice. The WHT goes to WHT receivable, and the receipt shows
"awaiting certificate" until the customer's WHT credit note is recorded (number and date).

**Journals.** All posted with source `receivable`:

| Document | Debit | Credit |
| --- | --- | --- |
| Invoice | Receivables (total) | Income per line (net, with cost centre); VAT output; stamp duty payable |
| Credit note | Income per line; VAT output; stamp duty payable | Receivables (total) |
| Receipt | Bank account (cash); WHT receivable | Receivables (cash + WHT) |

A document dated in a closed or unopened month can't be issued (a person is there to
re-date it); unlike payroll and expense journals, nothing is held. Receivables journals
can't be reversed from the Journals screen: a receipt is reversed from the receipt (with a
reason, and its allocations are removed), and an invoice is corrected with a credit note.
The receivables, VAT, WHT and stamp duty account codes are editable settings, seeded from
the chart (1100, 2200, 1310, 1320, 2210, 2220) **(confirm)**.

**Allocation.** A receipt or a credit note can be allocated across a customer's open
invoices. What's left over stays as credit on the account and can be allocated later from
the customer page. A credit note raised against an invoice is allocated to it on issue, up
to the invoice's balance.

**Aging and statements.** Aging is by days past the due date as at a chosen date: current,
1–30, 31–60, 61–90 and 90+. Statements list invoices, credit notes, receipts and receipt
reversals for a date range, with an opening and a running balance, and download as a PDF.
Document and statement PDFs are drawn on demand from issued, unchangeable data (as tax
certificates are in ADR 0022), so no copy is stored.

**Reminders.** A nightly task emails customers with reminders turned on: a set number of
days before the due date (3), on the due date, then every set number of days while overdue
(7). The days are editable **(confirm)**. An invoice gets at most one reminder a day, and
staff can also email a reminder from the customer page.

**Default terms.** New customers get the default payment terms (30 days) **(confirm)**,
which can be changed for each customer.

**Tax returns.** VAT, WHT and stamp duty returns are monthly, due on the 21st of the
following month **(confirm, editable)**. Figures come from the ledger, not from
receivables alone, so manual journals and (from 10c) supplier bills count:

- VAT output is the net credit movement on VAT output in the month.
- VAT input is the net debit movement on VAT input.
- WHT is the net credit movement on WHT payable.
- Stamp duty is the net credit movement on stamp duty payable.

Journals with source `tax` (future payments and settlements of returns) are left out. Once
its month ends, a return is a draft with live figures. It's then marked filed (date and
reference), which freezes its figures, and then paid (date and reference). It shows as
overdue once its due date passes unfiled. If the ledger changes after filing, the return
shows the difference. In 10b no journal is posted when a return is marked paid;
tax payments arrive with banking (10d).

## Consequences

- Input VAT and WHT payable fill in once supplier bills arrive (10c); the returns already
  read them from the ledger.
- Customer reminders and statements are emailed without attachments, since the mail
  service doesn't send any yet.
- No inventory items: invoice lines are free text against an income account until
  Phase 11.
