# 0021. Payroll (Nigeria), first gate: pay, statutory settings, runs and payslips

- Status: Accepted
- Date: 2026-11-20

## Context

Brief section 9.7 and Phase 9. The phase has two gates (confirmed 2026-11-20): 9a covers
pay set-up, statutory settings, the calculation, runs with maker-checker and payslips;
9b covers the bank file, PAYE, pension and NHF schedules, the payroll journal, loans and
salary advances, and year-end tax certificates.

## Decision

**Module.** `app/modules/payroll`, using HR (people, hire and leaving dates, PFA),
attendance (approved overtime) and files (payslips and rent proofs), each through its
`service.py`.

**Pay set-up.** Components are earnings or deductions, a fixed amount or a percentage of
one fixed component, taxable or not and pensionable or not. Salary structures list
components with default amounts. Each person has an effective-dated pay record (pay
group, structure, amounts) and optional recurring deductions. All of it is
`compensation` class.

**Pay groups** are monthly (confirmed 2026-11-20); they split runs and approvals.
Everyone with a pay record belongs to one group.

**Statutory settings** are effective-dated sets (from 1 January 2026, the Nigeria Tax Act
2025 bands, confirm): PAYE bands, rent relief (20%, capped at ₦500,000), pension (8%
employee, 10% employer of pensionable earnings: basic, housing and transport), NHF
(2.5% of basic), NHIS (0% until confirmed), NSITF (1% of gross, employer), ITF (1%,
employer, reported in 9b), minimum wage (₦70,000, a warning only), overtime (1.5 ×
hourly basic, 173 hours a month). The set in force on the last day of the period
applies.

**Calculation, per person and month.**
- Proration for joiners and leavers by calendar days (confirmed 2026-11-20).
- Formula components take their percentage of the prorated base component.
- Approved overtime from attendance is paid; absences are not deducted (confirmed
  2026-11-20).
- Taxable pay is taxable earnings less employee pension, NHF and NHIS.
- PAYE is cumulative for the tax year (confirmed 2026-11-20): tax on year-to-date
  taxable pay less year-to-date rent relief, on the annual bands scaled to the months
  elapsed, less PAYE already deducted in approved runs this year. It is never negative.
- Rent relief uses only verified declarations (confirmed 2026-11-20).
- Every amount is rounded to the kobo, half up.

**Runs** belong to a pay group and a month: `draft` → `calculated` → `reviewed` →
`approved` → `posted` → `paid`. Each step is done by someone other than whoever did the
step before; whoever created the run can't post it or mark it paid (confirmed
2026-11-20). A reviewer or approver can send a run back to draft with a note.
Payslip PDFs are generated at approval and shown to employees once the run is paid
(confirmed 2026-11-20).

## Consequences

The pay record in force on the last day of the month is used for the whole month
(mid-month pay changes are not split). Loans, salary advances, statutory schedules and
the journal arrive in 9b.
