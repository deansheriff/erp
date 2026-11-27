# Procurement

Phase 11c (ADR 0030). Purchase orders, receiving and bills stay in Payables
(`docs/FINANCE.md`); procurement decides what to buy and from whom, and drafts the order.
Permissions are in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `procurement_settings` | One row: the amount over which three quotes are needed. |
| `procurement_tiers` | The approval tiers: line manager (always), Finance and Director, each with the amount it applies over and whether it's in use. |
| `purchase_requisitions`, `requisition_lines` | Requisitions (`PR-`): purpose, needed-by date, cost centre, and lines that are a stock item into a warehouse or a description with an optional account, each with a quantity and an estimated unit price excluding VAT. |
| `requisition_steps` | The approval steps a requisition needs, in order, and who decided each. |
| `rfqs`, `rfq_suppliers` | Requests for quotation (`RFQ-`), the suppliers invited, when each was emailed and who declined. |
| `supplier_quotes`, `supplier_quote_lines` | Each supplier's reply: reference, dates, delivery days, a price and VAT per line, and the supplier's document (file purpose `supplier_quote`). |
| `supplier_ratings` | Procurement's 1–5 rating of a purchase order, one per order. |

## The flow

1. **Requisition.** Any employee raises one for their department's cost centre. Submitting
   numbers it and sets its approval steps from the estimated total.
2. **Approval.** Steps are decided in order:
   - the line manager always;
   - a Finance Manager when the total is over the Finance tier;
   - a Director when it's over the Director tier.

   Each step needs a different person, and nobody decides their own requisition. When the
   requester has no line manager, someone allowed to step in (the Director role) decides
   that step. A rejection, which needs a reason, ends the requisition.
3. **RFQ.** A Procurement Officer invites suppliers on an approved requisition and emails
   them the RFQ. Each supplier gets the lines in the email body and the RFQ as a PDF
   attachment. Suppliers without an email address get the PDF another way.
4. **Quotes.** The officer records each reply. Every line needs a price; a supplier who
   can't supply is marked as declined.
5. **Award.** Over the quotes threshold, three quotes are needed first. The officer picks
   any quote, but one that isn't the lowest net total needs a reason.
   - If the quote's net total needs a higher tier than the requisition was approved for,
     the missing steps are added. The requisition goes back for approval before ordering.
6. **Order.** The officer makes a draft purchase order in Payables:
   - the requisition's lines at the quote's prices;
   - stock lines keep their item and warehouse;
   - the officer chooses an account for any free-text line without one;
   - the expected date is the date it's made plus the quote's delivery days.

   Someone other than the officer approves the order. Receiving and billing are as before.

## Supplier performance

Each measure is shown on its own, over the last 3, 6, 12 or 24 months; there's no combined
score.

| Measure | From |
|---|---|
| On-time delivery | Goods received notes dated on or before the order's expected date (orders with no expected date aren't counted). |
| Lines received in full | Order lines whose received quantity reached what was ordered. |
| Returned | Stock sent back on posted debit notes, as a share of what was received. |
| Bills within tolerance | Posted bill lines priced within the price tolerance of the order. |
| Quotes won | Awarded quotes out of all the supplier's quotes. |
| Rating | Procurement's 1–5 rating, given once an order is received in full or closed. |

## Defaults (confirm before go-live)

| Setting | Default |
|---|---|
| Finance step | Over ₦500,000 |
| Director step | Over ₦5,000,000 |
| Three quotes needed | Over ₦1,000,000 |
| Quote documents kept | Six years |

## Screens

Procurement → Requisitions (and each requisition), RFQs (each RFQ with its comparison, and
the form to record a quote), Supplier performance (and each supplier), Settings.
