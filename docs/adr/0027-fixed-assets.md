# 0027: Fixed assets

Status: proposed (Phase 10e), decisions confirmed 2026-11-26

## Context

The brief asks for an asset register, categories with a depreciation method (straight
line or reducing balance) and useful life, monthly depreciation posting, revaluation, and
disposal with a gain or loss. The chart already has cost accounts 1510 to 1550, one
accumulated depreciation account (1590) and depreciation expense (6500).

## Decisions

**Accounts.** Each cost account gets its own accumulated depreciation account: 1511,
1521, 1531, 1541 and 1551. 1590 stays for anything else. New accounts, all to confirm:
3300 Revaluation reserve, 4910 Gain or loss on disposal of assets, and 6910 Revaluation
losses. Finance settings name the last three, with a capitalisation threshold of ₦100,000
**(confirm)**.

**Categories** name a cost account, an accumulated depreciation account and an expense
account, and a method:

- straight line over a useful life in months;
- reducing balance at an annual rate;
- not depreciated (land).

The seeded categories are Buildings (50 years), Land (not depreciated), Plant and
machinery (10 years), Motor vehicles (4 years), Furniture and fittings (5 years) and
Computer equipment (3 years). All are **to confirm**.

**Registering an asset** (`FA-` numbers) posts nothing, because the purchase is already
in the ledger:

- **From a bill:** a posted bill line on a category's cost account, at or above the
  threshold, is offered on the register. Registering it takes the line's net cost, the
  bill date and the supplier. Each bill line makes one asset.
- **By hand:** Finance registers an asset directly, for opening balances or anything
  bought outside payables. A cost below the threshold only warns. An asset brought in
  from before go-live can carry its accumulated depreciation and the month it was
  depreciated to.

The register compares its cost with the ledger balance of each cost account, so
anything missing shows.

An asset copies its category's method, life or rate and accounts. These and the cost,
residual value, date and category can change only until depreciation is first posted.
An asset with no depreciation and no changes can be deleted.

**Depreciation.** A full month's charge falls in the month the asset is acquired, and
none in the month it's disposed of.

- **Straight line:** each month charges (carrying amount − residual) ÷ months left, so
  rounding evens out and the last month lands exactly on the residual.
- **Reducing balance:** each month charges carrying amount × annual rate ÷ 12, never
  below the residual.

A **run** covers one month. Months are run in order: the next run is the month after the
last posted one, or the first month any asset needs. A run charges each active asset for
every month not yet charged up to its month, which catches up assets registered late.

A daily task (01:45 UTC) drafts the next month's run once that month has ended. Finance
can also prepare or recalculate one by hand. Someone other than whoever prepared it posts
it (SoD rule `finance.asset_four_eyes`); a run the task prepared can be posted by anyone
allowed. Posting:

- recalculates the run and refuses if the result has changed since it was prepared;
- posts one journal (source `asset`) on the month's last day: debit each category's
  expense account by cost centre, credit its accumulated depreciation account.

**Revaluation** to a new value at a date (IAS 16, elimination method). It needs
depreciation posted up to the month before. Its journal:

- clears the accumulated depreciation against the cost account, then moves the cost
  account to the new value;
- an increase first reverses earlier revaluation losses on the asset through 6910, and
  the rest goes to the reserve 3300;
- a decrease first uses the asset's reserve, and the rest goes to 6910.

Depreciation then continues from the new value over the months left.

**Disposal** at a date, with proceeds into a chosen bank account, or none for a
write-off. It also needs depreciation posted up to the month before. Its journal:

- debits accumulated depreciation and the bank;
- credits the cost account;
- takes the difference to 4910 as a gain or loss;
- moves any revaluation reserve on the asset to retained earnings.

Proceeds are recorded without VAT. A sale that needs a VAT invoice is invoiced in
receivables, and its disposal records no proceeds.

**Approval.** One person records a revaluation or disposal, and someone else approves it
(the same SoD rule). Approval posts the journal. An asset has at most one change waiting.

**Permissions:** `finance.asset.read`, `finance.asset.manage` (categories, the register,
preparing runs, recording changes) and `finance.asset.approve` (posting runs and
approving changes). Asset journals are reversed only through the asset, never from the
Journals screen.

## Consequences

- Book depreciation only. Tax capital allowances (CITA) are left for later.
- The register and the ledger can differ when an asset purchase isn't registered. The
  register's ledger comparison shows where.
