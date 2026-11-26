# Inventory

Phase 11a (ADR 0028). Purchases and sales start moving stock in 11b. Permissions are in
`docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `inventory_units` | Units of measure (EA, BOX, CTN, PCK, KG, L, M seeded). |
| `item_categories` | The inventory, cost of sales, adjustments and issues-to-staff accounts. |
| `items` | Code (`ITM-00001` unless given), name, category, unit, valuation (FIFO or weighted average), tracking (none, lot, serial), reorder level and quantity. |
| `warehouses`, `warehouse_bins` | Where stock is kept (`MAIN` Main store seeded) and its bins. |
| `stock_lots`, `stock_serials` | Lot numbers with expiry; serial numbers and where each is. |
| `stock_balances` | Quantity per item, warehouse, bin and lot. Never negative. |
| `item_values`, `cost_layers` | Quantity and value per item and warehouse; FIFO layers by receipt and lot. |
| `stock_documents`, `stock_document_lines` | Issues (`SI-`), transfers (`ST-`) and adjustments (`SA-`). |
| `stock_counts`, `stock_count_lines` | Counts (`SC-`): expected, counted, and the variance value posted. |
| `material_requests`, `material_request_lines` | Requests (`MR-`) and what has been issued against each line. |
| `stock_moves` | The stock ledger: one line per item and place a document moved, with its value and journal. |
| `low_stock_alerts` | Items and warehouses already in a day's low-stock digest. |

## Valuation

Valuation is per item and warehouse:

- **Weighted average** keeps the quantity and value. Stock goes out at value ÷ quantity,
  and the last unit out takes whatever value is left.
- **FIFO** takes the oldest receipt layers first, within the lot for lot items.

Bins don't carry cost. A lot item moved without a lot takes the earliest-expiring lots
first. Serial items move by serial number, one per unit.

## Movements and the ledger

| Movement | Journal (source `stock`) | Posted by |
|---|---|---|
| Issue | Debit the expense account (the category's issue account unless another is chosen) by cost centre; credit inventory | Inventory staff |
| Transfer | None: value moves at cost between warehouses or bins | Inventory staff |
| Adjustment in | Debit inventory; credit the contra account (the category's adjustment account unless another is chosen, e.g. for opening stock) | Someone other than whoever recorded it |
| Adjustment out | Debit the contra account; credit inventory at cost | Someone other than whoever recorded it |
| Count variance | Through each category's adjustment account | Someone other than whoever started the count |
| Goods received (`GRN-`) | Debit inventory at the order price; credit 2030 goods received not invoiced (source `payable`) | Whoever records the receipt in Finance |
| Debit note (`DN-`) | Credit inventory at cost; the bill difference to the category's adjustment account | Someone other than its drafter |
| Invoice (`INV-`) | Debit the category's cost of sales; credit inventory at cost | Whoever issues it |
| Credit note (`CN-`) | Debit inventory at the invoice's cost; credit cost of sales | Someone other than its drafter |

Finance moves stock through `app/modules/finance/stock_link.py`, which this module
implements in `service.py` (ADR 0029); the stock ledger shows the finance document's
number.

Stock journals are reversed only by another stock document, never from the Journals
screen.

## Counts

Starting a count records what each place in the warehouse holds. People enter what they
counted; lines can be added for stock found elsewhere. Once every line is counted, it's
submitted. The variance (counted − expected) posts when someone else approves: gains at
the current unit cost, losses at cost. Serial items are corrected by an adjustment with
their serial numbers instead.

## Material requests

1. Any employee raises a request for items from a store, with what it's for.
2. It goes to their line manager on the position tree. With no manager, it goes to
   anyone allowed to decide any request (the Inventory Manager). Nobody decides their own.
3. Once approved, inventory staff issue it, in parts if needed. Each issue is an issue
   document charged to the requester's department cost centre.

## Low stock

Every day at 07:00 UTC, everyone allowed to manage items gets one email listing active
items at or below their reorder level in each warehouse. An item and warehouse appears at
most once a day. The items page shows the same list.

## Defaults (confirm before go-live)

- Category General: 1200 Inventory, 5100 Cost of goods sold, the new 5200 Stock
  adjustments and variances, and 6300 Office supplies for issues.
- Units EA, BOX, CTN, PCK, KG, L and M.
- Warehouse MAIN, Main store.

## Screens

Inventory → Items (and each item), Movements (and each issue, transfer and adjustment),
Counts (and each count sheet), Material requests (and each request), Setup.
