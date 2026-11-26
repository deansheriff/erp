# 0025: Payables

Status: proposed (Phase 10c), decisions confirmed 2026-11-22

## Context

The brief asks for suppliers; purchase orders → goods received → supplier invoices with a
three-way match; payments with approval; debit notes; and aging. Requisitions, RFQs,
quote comparison and tiered approval belong to Procurement (Phase 11), which will feed
purchase orders into this flow.

## Decisions

**Suppliers** (`S-0001`) are a company or an individual, which picks the WHT rate. Each
has payment terms, an optional default WHT type, and bank details (bank name, CBN code,
NUBAN account number and account name) for the payment file. Finance doesn't use the
payroll module's bank list, because modules only talk through services and finance
imports none. The CBN code is typed in and checked to be 3–6 digits.

**Purchase orders** (`PO-`) are drafted by one person and approved by another (SoD rule
`finance.purchase_four_eyes`). Lines are against expense or asset accounts, with a VAT
code, like sales lines. A PO is draft, then approved, then part received or received,
then closed when fully billed. A PO can be cancelled before anything is received.

**Goods received** (`GRN-`) records quantities received against an approved PO's lines,
never more than is outstanding. Nothing posts: stock accounting arrives with Inventory.

**Bills** (`BILL-` inside the system, plus the supplier's own invoice number, unique per
supplier):

- **From a PO**, a bill's lines point at PO lines. **Direct bills**, for rent, utilities
  or services, are against expense or asset accounts.
- Every bill is drafted by one person and approved by another.
- **Three-way match on approval.** The quantity billed so far on each PO line can't be
  more than was received. The price can be up to the tolerance (2%, to confirm, editable)
  away from the PO price.
- A bill that fails the match is **held**. A Finance Manager (`finance.bill.override`)
  posts it with a reason.
- Posting debits each line's account (net, with cost centre) and VAT input, and credits
  trade payables (2010) with the total.
- Bills dated in a closed or unopened month can't be posted.

**Debit notes** (`DN-`) are raised against a posted bill, drafted by one person and issued
by another. Issuing posts debit payables and credit the lines' accounts and VAT input, and
reduces the bill's balance.

**Payments** (`PAY-`) settle a supplier's posted bills:

- **WHT is deducted at payment**, from each bill's WHT type and the supplier's rate
  (company or individual). It's charged on the part of the amount settled that isn't VAT:
  settled × net ÷ total × rate, rounded to the kobo.
- If a bill's WHT type has no rate set for that kind of supplier, the payment can't be
  prepared until Finance enters it.
- Prepared by one person and approved by another (SoD rule `finance.payment_four_eyes`).
- Approval posts debit payables with the amount settled, credit the bank with the cash
  paid, and credit WHT payable (2210) with the WHT.
- An approved payment downloads as the same generic CSV the payroll bank file uses
  (account name, account number, bank code, bank, amount, narration).
- It can be reversed with a reason (`finance.payment.approve`): its journal is reversed
  and the bills it settled are open again.

A debit note always applies to its bill and can't be more than the bill's balance, and a
payment is allocated in full to the bills it settles. So, unlike receivables, nothing is
ever left as credit on a supplier's account.

**Aging** of what is owed works as for receivables: current to 90+ days past the bill's
due date.

**Accounts and settings**: trade payables 2010 and the price tolerance (2%) are editable
settings **(confirm)**. Journals post with source `payable`, and like receivables are
reversed from their document, never from the Journals screen.

## Consequences

- WHT payable and VAT input now fill the tax returns from real documents.
- WHT credit notes the business issues to suppliers after remitting WHT aren't produced
  yet.
- No partial approval: a held bill is posted in full with a reason, or corrected.
