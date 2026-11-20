# Payroll (Nigeria)

Phase 9a. Decisions are in ADR 0021; permissions in `docs/PERMISSIONS.md`.

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
| `payslips`, `payslip_lines` | Each person's calculated pay for a run and its component lines. |

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

## Screens

Payroll → Runs (and each run), People's pay, Salary structures, Rent relief, Settings;
self-service: My payslips.
