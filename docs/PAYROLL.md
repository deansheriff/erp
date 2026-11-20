# Payroll (Nigeria)

Phase 9. Decisions are in ADR 0021 (9a) and ADR 0022 (9b); permissions in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `pay_groups` | Monthly pay groups (e.g. Staff, Management). |
| `pay_components` | Earnings and deductions: fixed or a percentage of a fixed component, taxable, pensionable. |
| `salary_structures`, `salary_structure_components` | Named structures and their components with default amounts. |
| `employee_pay`, `employee_pay_amounts` | A person's effective-dated pay: group, structure, PFA, amounts per component. |
| `employee_deductions` | Recurring deductions with a first and (optional) last month. |
| `statutory_sets`, `tax_bands` | Effective-dated statutory rates and the PAYE bands. |
| `rent_declarations` | Annual rent declared per tax year with proof, and its verification. |
| `payroll_runs` | One run per pay group and month, with who did each step. |
| `payslips`, `payslip_lines` | Each person's calculated pay for a run and its component lines (loan repayments point at their loan). |
| `staff_loans` | Interest-free loans and salary advances (`LN-00001`), repaid by monthly instalments. |
| `payroll_banks` | Banks and their CBN codes, with the other names HR records use. |
| `payroll_accounts` | Ledger account codes for payroll journals (one row). |
| `payroll_journals`, `payroll_journal_lines` | Balanced journals queued for the ledger: accrual at posting, payment when paid, disbursement when a loan is approved. |

## Defaults (confirm before go-live)

- PAYE (Nigeria Tax Act 2025, from 1 January 2026): first ₦800,000 at 0%, next
  ₦2,200,000 at 15%, next ₦9,000,000 at 18%, next ₦13,000,000 at 21%, next ₦25,000,000
  at 23%, above ₦50,000,000 at 25%.
- Rent relief 20% of annual rent, capped at ₦500,000.
- Pension 8% employee, 10% employer; NHF 2.5% of basic; NHIS 0%; NSITF 1%; ITF 1%.
- Minimum wage ₦70,000 (warning only); overtime 1.5 × hourly basic; 173 hours a month.
- Components: Basic, Housing, Transport (taxable, pensionable), Meal allowance
  (taxable), Overtime (taxable), Cooperative and Union dues (deductions).
- Pay group: Staff.
- Ledger accounts: 6000 salaries, 6010 employer pension, 6020 employer NSITF, 2110 PAYE,
  2120 pension, 2130 NHF, 2140 NHIS, 2150 NSITF, 2160 other deductions, 2170 net
  salaries payable, 1420 staff loans, 1010 bank.
- Bank codes for the common Nigerian banks and the larger mobile-money banks.

## Remittances (ADR 0022)

From an approved, posted or paid run, as CSV (each download audited, and needing
`payroll.remittance.export`):

- bank payment file: account name, account number, CBN bank code, bank, net pay,
  narration. People without a 10-digit account number or a listed bank are left out
  and named on the page;
- PAYE per state of residence, with TIN, gross, taxable pay and PAYE;
- pension per PFA, with RSA PIN and the employee and employer contributions;
- NHF, with NHF number, basic pay and the deduction.

## Loans and salary advances

Interest-free. A Payroll Officer records the amount, monthly instalment and first
month; a Finance Manager (not the recorder, not the borrower) approves or rejects it.
Approved loans are deducted in each run while a balance remains; the last instalment is
whatever is left. A loan can be withdrawn until it is approved.

## Tax certificates

A PDF per person and tax year with gross pay, pension, NHF and NHIS, taxable pay and
PAYE by month, and rent relief. Payroll downloads them at any time; employees from My
payslips once the December run that pays them is paid (or once the year is over, if
they left before December).

## Screens

Payroll → Runs (and each run), People's pay, Remittances, Loans, Tax certificates,
Salary structures, Rent relief, Settings (with ledger accounts and bank codes);
self-service: My payslips (with loans and tax certificates).
