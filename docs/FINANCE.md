# Finance

Phase 10, in five gates. 10a, the general ledger, is in ADR 0023; 10b, receivables and
tax, is in ADR 0024; 10c, payables, is in ADR 0025; 10d, banking, is in ADR 0026; 10e, fixed assets, is in ADR 0027; permissions are in
`docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `finance_accounts` | The chart of accounts: code, name, type, parent header, cash-flow class. |
| `fiscal_years`, `fiscal_periods` | Financial years (January to December) and their twelve months, open or closed. |
| `journals`, `journal_lines` | Drafted, held and posted journals and their lines (account, cost centre, debit, credit, currency). |
| `recurring_journals`, `recurring_journal_lines` | Templates drafted on their day each month. |
| `finance_settings` | The retained earnings account; the receivables, VAT, WHT and stamp duty accounts; default terms, reminder days and the return due day (one row). |
| `customers` | Code (`C-0001`), name, email, address, TIN, payment terms, reminders on or off. |
| `sales_documents`, `sales_lines` | Quotes, sales orders, invoices and credit notes, and their lines (income account, cost centre, quantity, price, VAT code, rate and amount). |
| `receipts` | Cash received and WHT deducted, the bank account, the WHT credit note, and any reversal. |
| `sales_allocations` | Parts of receipts and credit notes set against invoices. |
| `vat_rates`, `wht_types`, `stamp_duty_types` | Effective-dated VAT, withholding-tax types and rates, stamp duty types. |
| `suppliers` | Code (`S-0001`), company or individual, TIN, terms, usual WHT type, bank name, CBN code, account number and name. |
| `purchase_orders`, `purchase_lines` | Purchase orders and their lines (cost account, quantity, price, VAT). |
| `goods_receipts`, `goods_receipt_lines` | Goods received against PO lines. Nothing posts. |
| `bills`, `bill_lines` | Supplier bills and debit notes, with the supplier's invoice number and the PO lines they match. |
| `supplier_payments`, `supplier_payment_lines` | Payments, what each settles on a bill, and the WHT withheld. |
| `tax_returns` | Filed and paid monthly VAT, WHT and stamp duty returns, with their figures as filed, the bank paid from and the payment journal. |
| `bank_accounts` | Each bank account's own ledger account (1010, then 1011 to 1099), bank name, CBN code, account number and name, and its CSV or Excel statement layout. |
| `bank_statements`, `statement_lines` | Imported statement files and their lines (signed amount, a fingerprint so a line imports once, matched journal line or entry, reconciliation). |
| `bank_rules` | Description-contains rules that give unmatched lines an entry account. |
| `bank_reconciliations` | One per bank account and statement date: the statement balance, prepared, submitted and approved by. |
| `bank_transfers` | Transfers between bank accounts, recorded by one person and approved by another. |
| `asset_categories` | Depreciation method (straight line, reducing balance or none), useful life or annual rate, and the cost, accumulated depreciation and expense accounts. |
| `fixed_assets` | The register (`FA-00001`): cost, cost or valuation in the ledger, residual, accumulated depreciation, months left, the month depreciated to, revaluation reserve, and the bill line it came from. |
| `asset_changes` | Revaluations and disposals, recorded by one person and approved by another, with the figures at approval. |
| `depreciation_runs`, `depreciation_lines` | One run per month and what it charged each asset. |

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
Receivables 1100, VAT output 2200, VAT input 1310, WHT receivable 1320, WHT payable 2210,
stamp duty payable 2220. Payment terms 30 days; reminders 3 days before, on the due date,
then every 7 days; returns due on the 21st. Trade payables 2010; bill price tolerance 2%.
Bank matching within 3 days of the ledger date; statement files kept 6 years.
Goods received not invoiced: 2030.
Fixed assets: accumulated depreciation 1511 to 1551 (one per cost account), revaluation
reserve 3300, gain or loss on disposal 4910, revaluation losses 6910, capitalisation
threshold ₦100,000; categories Buildings 50 years, Land not depreciated, Plant and
machinery 10 years, Motor vehicles 4 years, Furniture and fittings 5 years, Computer
equipment 3 years, all straight line.

## Receivables

- **Customers** get the default payment terms (30 days) unless changed.
- **Quotes → orders → invoices**, every step optional: converting copies the lines into a
  new draft. Quotes are sent, then accepted or declined; orders are confirmed, then
  invoiced or cancelled.
- **Invoices** are issued by whoever drafts them. Prices exclude VAT; each line is
  standard-rated, zero-rated or exempt, and VAT is worked out per line at the rate in
  force on the invoice date. An optional stamp duty type adds a flat amount or a
  percentage of the net. Issuing posts debit receivables, credit income, VAT output and
  stamp duty payable. An invoice dated in a closed or unopened month can't be issued.
- **Stock lines** (ADR 0029): a sales line can name a stock item and a warehouse (and
  serial numbers for serial items). Issuing an invoice takes the stock out at cost,
  earliest-expiring lot first, and adds debit cost of sales, credit inventory to its
  journal; it's refused if there isn't enough on hand. A credit note puts the stock
  back at the cost its invoice took it out at (or the current cost without an invoice),
  debit inventory, credit cost of sales.
- **Credit notes** are drafted by one person and issued by another
  (`finance.credit_note.issue`), and post the invoice's journal the other way round. One
  raised against an invoice is allocated to it, and credit notes can't come to more than
  the invoice.
- **Receipts** record cash received and any WHT the customer deducted; both settle
  invoices. They post debit the bank and WHT receivable, credit receivables. What isn't
  allocated stays as credit, to allocate later from the customer page. A receipt is
  reversed with a reason (`finance.receipt.reverse`): its journal is reversed and the
  invoices it settled are open again. The customer's WHT credit note is recorded on the
  receipt.
- Receivables journals are never reversed from the Journals screen.
- **Aging** as at a date: current, 1–30, 31–60, 61–90 and 90+ days past due, by
  customer, with CSV. **Statements** for a date range, on screen and as a PDF. Invoices,
  quotes, orders and credit notes download as PDFs, drawn on demand.
- **Reminders** are emailed by a daily task (08:00 UTC, 09:00 in Lagos) 3 days before the
  due date, on the due date, and every 7 days while overdue, one email per customer with
  the invoices due a reminder. An invoice gets at most one a day. Staff can also email
  one from the customer page.

## Projects on the ledger

Journal lines can name an active project (ADR 0031), like a cost centre.

- **Where:** manual journals, invoice and credit note lines (with the cost of stock
  sold), purchase order lines and the bills made from them, direct bill lines, expense
  claims and stock issues.
- **Closed projects:** bills against an order keep its project even after the project
  closes.
- **Recurring templates:** they don't name projects.

Project profitability is read from these lines (`docs/PROJECTS.md`).

## Vehicles on the ledger

Journal lines can also name a vehicle that isn't retired (ADR 0033).

- **Where:** manual journals, direct bill lines (and the debit notes made from them) and
  expense claims.
- **Merging:** lines are merged by account, cost centre, project and vehicle.
- **Recurring templates:** they don't name vehicles.

Cost per vehicle is read from these lines (`docs/FLEET.md`).

## Payables

- **Purchase orders** (`PO-`) are drafted by one person and approved by another. Goods
  received (`GRN-`) are recorded against an approved PO's lines, never more than is still
  to come. Lines that aren't stock post nothing until the bill.
- **From procurement** (ADR 0030): awarding a supplier's quote on an approved requisition
  makes a draft purchase order with the quote's prices, approved here as usual.
- **Stock lines** (ADR 0029): a PO line can name a stock item and a warehouse; it's
  charged to the item's inventory account. Receiving it brings the stock in at the order
  price (with a bin, lot and expiry, or serial numbers, as the item needs) and posts debit
  inventory, credit goods received not invoiced (2030), so the date must be in an open
  month.
- **Bills** (`BILL-`) come from a PO (its received, unbilled lines) or are direct, against
  expense or asset accounts. Each records the supplier's invoice number, once per
  supplier. Every bill is approved by someone other than its drafter. A bill from a PO is
  matched on approval: quantities billed can't be more than received, and prices can be
  up to the tolerance (2%) away from the PO. One that fails is **held**; a Finance Manager
  (`finance.bill.override`) posts it with a reason, or it is corrected and approved again.
  Posting debits the lines' accounts and VAT input and credits trade payables (2010).
  A stock line instead debits goods received not invoiced at the order price, with any
  price difference to the item's variance account (5200); stock keeps its received
  cost.
- **Debit notes** (`DN-`) are raised against a posted bill, issued by someone other than
  the drafter, and can't be more than the bill's balance. Stock lines go back to the
  supplier from the order line's warehouse (serial items name their serials): inventory
  is credited at cost, the difference goes to the variance account, and the quantity no
  longer counts as received.
- **Payments** (`PAY-`) settle posted bills. WHT is withheld at payment from each bill's
  WHT type at the supplier's company or individual rate, on the part settled that isn't
  VAT. A payment is prepared by one person and approved by another; approval posts debit
  payables, credit the bank and WHT payable (2210). An approved payment downloads as the
  generic bank CSV payroll uses, and can be reversed with a reason.
- **Payables aging** by days past the bill's due date, with CSV.

## Banking

- **Bank accounts** each have their own ledger account under 10 Cash and bank; 1010 is the
  first. Adding one creates the next code from 1011. Balances come from the ledger.
- **Statements** import from CSV or Excel (the account's layout: a header or column
  number for date, description, reference and a signed amount or money in and out, the
  date format and rows to skip) or OFX. The file is kept (6 years, to confirm). A line
  already imported is skipped by its fingerprint.
- **Matching:** a line with exactly one unmatched ledger entry on the bank account for the
  same amount within 3 days (editable) is matched on import. Otherwise the first rule
  whose text the description contains (and direction fits) gives it an entry account.
  The rest are matched by hand from suggestions or given an entry. Entries post when the
  reconciliation is approved.
- **Reconciliation** to a statement date after the last approved one: the ledger balance
  at that date, less ledger entries not yet on a statement, plus entries waiting to post,
  must equal the statement balance with no unmatched lines up to the date. It is
  submitted by one person and approved by another (`finance.bank_four_eyes`). Approval
  posts the entries (source `bank`) in an open month and locks the lines.
- **Transfers** (`TRF-`) between bank accounts are recorded by one person and approved by
  another; approval debits the receiving account and credits the paying one.

## Fixed assets

- **Categories** give the method (straight line over a useful life, reducing balance at an
  annual rate, or not depreciated) and the cost, accumulated depreciation and expense
  accounts. An asset copies them when registered.
- **Registering** posts nothing, because the purchase is already in the ledger. A posted
  bill line on a category's cost account, at or above the threshold, is offered on the
  register and brings its cost, date and supplier. Anything else is registered by hand,
  with any depreciation from before go-live. The register shows its cost or valuation
  against each cost account's ledger balance.
- **Depreciation:** a full month's charge in the month acquired, none in the month of
  disposal. Straight line spreads what is left above the residual over the months left;
  reducing balance takes the annual rate a month at a time, never below the residual.
  One run per month, in order, catching up any month an asset missed. The nightly task
  (01:45 UTC) prepares the next month once it has ended, and someone other than the
  preparer posts it: debit expense by cost centre, credit accumulated depreciation.
- **Revaluations** (IAS 16, elimination method) and **disposals** need the depreciation
  up to the month before. Each is recorded by one person and approved by another,
  which posts it. An increase reverses earlier revaluation losses (6910), then goes to
  the reserve (3300); a decrease uses the reserve, then goes to 6910. A disposal clears
  cost and depreciation, takes any proceeds into a bank account, puts the gain or loss
  in 4910, and moves the asset's reserve to retained earnings.

## Tax

- **VAT** 7.5% from 1 February 2020, effective-dated (confirm). Issued invoices keep their
  rate.
- **Withholding tax** types are seeded without rates; Finance enters company and
  individual rates.
- **Stamp duty** types are added by Finance, as a flat amount or a percentage.
- **Returns** are monthly, due on the 21st of the following month (confirm). Figures come
  from the ledger: VAT output is the net credit on 2200, VAT input the net debit on
  1310, WHT the net credit on 2210, and stamp duty the net credit on 2220. A return is a
  draft with live figures, then filed (frozen, with a reference), then paid. A filed
  return shows if the ledger has moved since. Marking a return paid needs the bank it was
  paid from and posts the payment (source `tax`): VAT clears output against input and
  pays the net from the bank; WHT and stamp duty pay their liability.

## Reports

Trial balance and balance sheet as at a date; profit and loss (by cost centre if wanted),
cash flow (indirect, by each balance-sheet account's cash-flow class; unclassified ones
count as operating) and the general ledger for an account, over a date range. The
year-end closing journal is left out of profit and loss and cash flow. Trial balance and
general ledger download as CSV.

## Screens

Finance → Customers (and each customer), Sales (and each document and receipt), Aging,
Suppliers (and each supplier), Purchases (and each PO, bill and payment), Payables aging,
Banking (and each bank account and reconciliation), Transfers, Fixed assets (and each
asset, its form, the categories and the depreciation runs),
Journals (and each journal), Chart of accounts, Recurring, Periods, Reports, Tax returns,
Tax settings.
