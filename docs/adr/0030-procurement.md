# 0030: Procurement

Status: proposed (Phase 11c), decisions confirmed 2026-11-26

## Context

The brief (9.10) asks for this flow:

1. Purchase requisitions.
2. Approval tiered by amount.
3. RFQs to suppliers, and comparing their quotes.
4. A purchase order, linked to Payables for receiving and invoices.

It also asks for a history of each supplier's performance.

## Decisions

**Module.** The work goes in `app/modules/procurement`.

- It uses finance only through `finance/service.py`, for suppliers, cost accounts, drafting
  purchase orders and each supplier's order history.
- It uses inventory only through `inventory/service.py`, for stock items and warehouses.

**Requisitions (`PR-`).** Every employee can raise one, for their department's cost centre.

- A line is either a stock item with a warehouse, or a description with an optional
  expense account.
- Each line has a quantity and an estimated unit price, excluding VAT. Approval is decided
  on the estimated total.

**Approval tiers.** The approvers form a cumulative chain set by an editable tier table.
These defaults are to be confirmed:

| Step | Needed when the total is | Decided by |
|---|---|---|
| Line manager | Always | The requester's line manager |
| Finance | Over ₦500,000 | A Finance Manager |
| Director | Over ₦5,000,000 | The new Director role |

How the chain runs:

- Steps are decided in order, and a rejection at any step ends the requisition.
- Each step needs a different person, and nobody approves their own requisition.
- When the requester has no line manager, someone with "step in on requisitions" decides
  the line-manager step. The Director role and the organisation administrator have it.

**RFQs (`RFQ-`).** A Procurement Officer invites suppliers to quote on an approved
requisition.

- Each supplier is emailed the RFQ as a PDF attachment. This needs attachment support in
  the mail outbox, which doesn't exist yet.
- The officer records each reply: a price for every line, the VAT code, delivery days,
  validity, the supplier's reference, and optionally the supplier's document.

**Quotes needed.** A requisition over an editable threshold needs at least three quotes:
the default threshold is ₦1,000,000, to confirm. Below it, one quote is enough.

**Award.** The officer picks any quote. If it isn't the lowest total, they must give a
reason, which is kept with the comparison. Totals are compared net of VAT, like the
requisition's estimates: VAT on purchases is usually reclaimed, so it shouldn't decide the
award.

- If the quote's total needs a higher tier than the requisition was approved for, the
  requisition goes back for the missing approvers before an order can be made.
- If it stays within the approved tier, the order can be made at once.

**Purchase order.** Making the order creates a draft PO in Payables:

- it has the requisition's lines at the quote's prices;
- stock lines keep their item and warehouse;
- the officer chooses an account for any free-text line without one;
- the expected date is the date it's made plus the quote's delivery days.

Someone other than the officer approves it, as for any PO. Receiving and billing are
unchanged (ADR 0025, 0029).

**Supplier performance.** Each supplier's history shows these measures separately, with
no combined score:

- **On-time delivery:** goods received notes dated on or before the PO's expected date.
- **Quantity accuracy:** order lines received in full, and stock returned on debit notes.
- **Price accuracy:** bill lines within the price tolerance of the PO, and how often the
  supplier's quotes win.
- **Manual rating:** a score from 1 to 5 with a comment, one per order, given by
  procurement once the order is received or closed.

**Permissions.**

- Requisitions: `procurement.requisition.create`, `.read`, `.approve` (line manager),
  `.approve_any` (step in), `.approve_finance` and `.approve_director`.
- RFQs: `procurement.rfq.read` and `procurement.rfq.manage`.
- Settings: `procurement.settings.manage`.
- Ratings: `procurement.supplier.rate`.

Who gets them:

- The existing Procurement Officer template gets the RFQ, rating and supplier permissions,
  plus `finance.purchase.create`.
- A new Director role approves the top tier and steps in.
- Finance Managers approve the finance tier and keep the settings.

## Consequences

- The mail outbox gains an optional attachment.
- The screens were requested from Stitch while Stitch was slow to deliver. Until a screen's
  design arrives, it's built from the design system, and its `screens.yaml` entry is marked
  `stitch_pending`. The CI rule then accepts it without an export.
- New screens: requisitions, requisition, RFQs, RFQ (comparison and award), record a
  quote, supplier performance, one supplier's performance, and procurement settings.
