# 0022. Payroll, second gate: remittances, the journal, loans and tax certificates

- Status: Accepted
- Date: 2026-11-20

## Context

Brief section 9.7, Phase 9b (the second payroll gate, after ADR 0021): a bank payment
file, PAYE schedules per State Internal Revenue Service, pension schedules per PFA, the
NHF schedule, the payroll journal to the GL, loan and salary-advance repayments, and
year-end tax certificates.

## Decision

**Remittances** are downloads from an approved, posted or paid run, as CSV:
- the bank payment file, one row per person: account name, account number, CBN bank
  code, bank, amount (net pay), narration (confirmed 2026-11-20: a generic CSV that bank
  portals can map). People without a recognised bank or a valid account number are
  left out and listed;
- PAYE per state of residence (people without a state are listed under "Not set"), with
  TIN, taxable pay and PAYE;
- pension per PFA, with RSA PIN and the employee and employer contributions;
- NHF, with NHF number and the deduction.

They carry encrypted HR identifiers, so they need `payroll.remittance.export`, and each
download is audited. NSITF and ITF are shown as employer totals (ITF is an annual
return, reported only).

**Bank codes.** HR keeps the bank as free text, so payroll keeps a list of banks with
their CBN codes and other names they go by, matched without regard to case or spacing.
The common banks are seeded (to confirm) and the list is edited under Payroll settings.

**The journal.** Posting a run queues an accrual journal for the ledger (Phase 10):
salaries and employer pension and NSITF as expenses, by each person's cost centre, against
PAYE, pension (employee and employer), NHF, NHIS, NSITF, other deductions, staff loans
and net salaries payable. Marking the run paid queues the payment journal: net salaries
payable against the bank. Approving a loan queues its disbursement: staff loans against
the bank. Account codes are settings with defaults to confirm.

**Loans and salary advances** are interest-free (confirmed 2026-11-20): an amount repaid
by a fixed monthly instalment from a first month until cleared; the last instalment is
whatever is left. A Payroll Officer records one and a Finance Manager approves or
rejects it (confirmed 2026-11-20); nobody approves a loan they recorded or their own.
Approved loans are deducted in each run from the first month while a balance remains;
the balance counts repayments in approved, posted and paid runs. A loan can be withdrawn
until it is approved (afterwards its disbursement journal is queued, so it stands).
Employees see their loans on My payslips.

**Year-end tax certificates** are PDFs per person and tax year: pay, pension, NHF, NHIS,
rent relief, taxable pay and PAYE by month, from approved runs. Payroll can download
them at any time; employees download their own once every December run of the year
that pays them is paid (confirmed 2026-11-20).

## Consequences

Early or lump-sum loan repayments are not recorded in payroll; a loan can only be
cleared by instalments. Remittance files are plain CSV; bank-specific layouts, if a bank
rejects the generic file, are later work.
