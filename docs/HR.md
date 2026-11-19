# HR core and self-service

Phase 6. Decisions are in ADR 0017; permissions in `docs/PERMISSIONS.md`.

## Employee fields

| Class | Fields | Read | Change |
|---|---|---|---|
| directory | employee number, names, work email, status, branch, positions | `hr.employee.read` | `hr.employee.update` |
| personal (self-service) | phone, personal email, residential address, state of residence, marital status, next of kin (name, relationship, phone, address) | `hr.employee.personal.read` | `hr.employee.personal.update`; the employee directly |
| personal (HR-managed) | date of birth, gender, state of origin, employment type, NYSC service dates, hire date, confirmation date, probation end | `hr.employee.personal.read` | `hr.employee.personal.update`, never on your own record |
| statutory | NIN\*, TIN\*, pension PFA, RSA PIN\*, NHF number\* | `hr.employee.statutory.read` | `hr.employee.statutory.update`; the employee by request |
| banking | bank name, account number\*, account name, BVN\* | `hr.employee.banking.read` | `hr.employee.banking.update`; the employee by request |

\* Encrypted at rest. `compensation` and `sensitive` are used by documents now and by
payroll and discipline later.

## Tables

| Table | What it holds |
|---|---|
| `employees` | The fields above, plus the sign-in account link. |
| `number_sequences` | Gap-free counters: key, prefix, padding, next value (`employee`: `EMP-`, 5). |
| `employee_change_requests` | Employee, class, changed field names, encrypted new values, status (`pending`, `approved`, `rejected`, `cancelled`), requester, decider, note. |
| `employee_document_categories` | Name, field class, active flag. Seeded: Employment contract (compensation), Identity document (statutory), Certificate (personal), Appraisal letter (personal), Medical report (sensitive) (confirm). |
| `employee_documents` | Employee, category, title, the file's version group, who added it. |
| `checklist_templates` / `checklist_template_tasks` | Onboarding or offboarding; optional department or position; tasks with assignee (`hr`, `it`, `manager`, `employee`) and due offset in days. |
| `checklists` / `checklist_tasks` | A started checklist for one employee, with the manager fixed at the start, and its tasks with due dates and completion. |
| `employee_exits` | Kind (`resignation`, `termination`), reason, notice date, last working day, final pay due, status (`scheduled`, `revoked`, `cancelled`). |

## Flows

**Add and onboard.** HR adds the employee (number allocated) → starts onboarding (the
most specific template: position, then department, then default) → invites them to sign
in (account linked, Employee role given).

**Self-service.** `/me` shows the employee's own record, documents, requests and tasks.
Contact and next-of-kin changes save at once. Statutory and banking changes wait for HR.

**Exit.** HR records the exit → offboarding checklist starts, anchored on the last
working day → at midnight Lagos time after the last working day the beat task revokes
access (account deactivated; sessions, API keys, delegations, role assignments and
positions ended; record `terminated`).

## Screens

HR → People, Employee profile, Add/edit employee, Change requests, Onboarding and
offboarding, HR settings. Everyone → Directory, My profile.
