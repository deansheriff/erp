# 0028: Inventory

Status: proposed (Phase 11a), decisions confirmed 2026-11-26

## Context

The brief asks for:

- items with units of measure, categories and reorder levels;
- warehouses and bins;
- receipts, issues, transfers and adjustments;
- lot and serial tracking where enabled, and valuation by FIFO or weighted average per
  item;
- stock counts with variance posting, material requests from staff, and low-stock alerts;
- stock movements that post to the general ledger.

Phase 11 has five gates. Inventory splits in two: **11a** is the stock itself and the
movements inventory owns. **11b** connects purchasing and sales: goods received into stock
through "goods received not invoiced", cost of sales when an invoice is issued, and
returns on credit and debit notes.

## Decisions

**Module boundaries.** Inventory is its own module, `app/modules/inventory`. It posts
journals through finance's public service (source `stock`) and never edits finance
tables. Finance never imports inventory. In 11b, finance calls inventory through a hook
that `app/wiring.py` connects.

**Items** have:

- a code;
- a category and a unit of measure, with one unit per item and no conversions;
- valuation by FIFO or weighted average;
- tracking: none, lot (with an optional expiry date) or serial;
- a reorder level and a reorder quantity.

**Categories** name four accounts, with defaults to confirm:

| Account | Default |
|---|---|
| Inventory | 1200 |
| Cost of sales | 5100, used in 11b |
| Adjustments and variances | new 5200 |
| Issues to staff | 6300 Office supplies |

**Warehouses and bins.** Stock sits in a warehouse, optionally in a bin, and for lot items
in a lot. Stock never goes negative: a movement that would take a location below zero is
refused, giving the quantity on hand.

**Valuation** is per item and warehouse:

- **Weighted average** keeps the quantity and value. Stock goes out at value ÷ quantity,
  and the last unit out takes whatever value is left.
- **FIFO** keeps cost layers by receipt (and by lot for lot items). Stock goes out from
  the oldest layers first.

Bins don't carry cost; a transfer moves value at its cost out.

**Lots and serials.**

- A lot item's issue or transfer without a lot takes the earliest-expiring lots first.
- Serial items move whole units, each with its serial number. A serial number is in
  stock at one place at a time.

**Movements** are documents, numbered when posted:

- **Issue** (`SI-`): to a cost centre, debiting an expense account (the category's issue
  account by default), credit inventory. Posted directly by inventory staff.
- **Transfer** (`ST-`): between warehouses or bins, at cost. No journal, because the
  inventory account doesn't change. Posted directly.
- **Adjustment** (`SA-`): adds stock at a unit cost, or removes it at its cost, against a
  contra account (the category's adjustment account, or another, like an opening-balance
  account). Someone other than whoever recorded it posts it (SoD rule
  `inventory.adjust_four_eyes`).

**Stock counts** (`SC-`) take a snapshot of what each location expects. People enter what
they counted, and someone else posts the variance through the adjustment account. The
variance is counted minus expected, so movements during a count must still be recorded.
Serial items can't post a quantity variance; they're corrected by an adjustment with
the serial numbers.

**Material requests** (`MR-`):

- Any employee can raise one for their own work.
- It goes to the requester's line manager from the position tree. When there's no
  manager, it goes to people allowed to decide any request. Nobody decides their own.
- Once approved, inventory staff issue it, in parts if needed. Each issue is an issue
  document charged to the requester's cost centre.

**Low stock.** Once a day (07:00 UTC), everyone allowed to manage items gets one email
listing items at or below their reorder level in each warehouse. An item and warehouse
is listed at most once a day. The items page shows the same list.

**Permissions:**

| Permission | What it allows |
|---|---|
| `inventory.item.read` | See items and stock |
| `inventory.item.manage` | Items, categories, units, warehouses and bins |
| `inventory.stock.move` | Record movements and counts, and issue requests |
| `inventory.stock.approve` | Post adjustments and counts someone else recorded |
| `inventory.request.create` | Raise requests (self) |
| `inventory.request.read` | See requests (self, team or organisation) |
| `inventory.request.approve` | Decide the requests you're named on (team) |
| `inventory.request.approve_any` | Decide any request (organisation) |

A new Inventory Manager role holds all of the stock and item permissions. The Inventory
Clerk records movements and counts.

## Consequences

- Units of measure have no conversions yet: an item is bought, held and issued in one
  unit.
- Purchases and sales don't move stock until 11b. Until then, stock comes in by
  adjustment, which also serves for opening balances.
- In-app notifications arrive with the notification service. Until then the low-stock
  digest is email plus the list on the items page.
