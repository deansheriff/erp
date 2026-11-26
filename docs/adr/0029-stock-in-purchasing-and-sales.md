# 0029: Stock in purchasing and sales

Status: proposed (Phase 11b), decisions confirmed 2026-11-26

## Context

Inventory (ADR 0028) holds stock and posts its own movements. Purchases and sales still
moved money only: goods received posted nothing, and bills and invoices knew nothing of
stock. The brief says stock movements post to the general ledger.

## Decisions

**Boundary.** Finance never imports inventory. Finance defines a small interface,
`app/modules/finance/stock_link.py`, with these operations:

- list stock items and warehouses;
- move stock in or out for one of its documents;
- say what a document took out;
- tie moves to the document's journal.

Inventory implements it in `app/modules/inventory/service.py`, and `app/wiring.py` connects
the two at start-up.

**Stock lines.** A purchase order line or sales line may name a stock item and a warehouse.

- On a purchase order, a stock line is charged to the item's inventory account, whatever
  account was typed.
- A sales line keeps its income account.
- Bills made from an order carry the item across. Direct bills (with no order) have no
  stock lines.

**Goods received.** Receiving a stock line brings the quantity into its warehouse at the
order's unit price (excluding VAT). It takes a bin, lot and expiry, or serial numbers,
where the item needs them. The goods received note now posts a journal (source
`payable`), in an open month:

- debit inventory at the received value;
- credit 2030 Goods received not invoiced (new, to confirm, a finance setting).

Lines that aren't stock post nothing, as before.

**Bills.** A bill line for a stock item has two parts:

- it debits GRNI at the received value, which is the quantity billed at the order price;
- any difference to the billed net goes to the item category's adjustments and
  variances account (5200).

Stock keeps its received cost, so FIFO layers never change afterwards.

**Debit notes** on stock lines return the stock to the supplier from the order line's
warehouse:

- inventory is credited at the stock's cost (FIFO or average);
- the difference to the note's net goes to 5200.

Lot items take the earliest-expiring lot first, and serial items need their serial
numbers on the note.

**Invoices.** Issuing an invoice with stock lines takes the stock out of each line's
warehouse at cost: earliest-expiring lot first, or by serial number. It's refused if
there isn't enough on hand. The same journal debits each category's cost of sales account
and credits inventory.

**Credit notes** put stock lines back into the warehouse:

- against an invoice, at the unit cost the invoice took it out at;
- otherwise, at the item's current cost.

The same journal debits inventory and credits cost of sales.

## Consequences

- Receiving stock now needs an open month.
- Goods received notes have a journal, and the stock ledger shows `GRN-`, `BILL-`/`DN-`
  and `INV-`/`CN-` numbers as well as inventory's own.
