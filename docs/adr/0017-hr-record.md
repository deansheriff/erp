# 0017. The HR record: field classes, self-service changes, onboarding and exits

- Status: Accepted
- Date: 2026-11-19

## Context

Brief sections 4.1 (field classes), 9.2, 9.3 and Phase 6. Phase 6 covers the employee
record with field-level visibility, employee documents, the colleague directory,
onboarding and offboarding with automatic access revocation, and the self-service
profile. Lifecycle changes, discipline, training, performance and recruitment move to a
later HR phase (confirmed by the product owner on 2026-11-19).

## Decision

**One record, classed fields.** The `employees` table keeps every HR field. Each field
belongs to one class (`app/modules/hr/fields.py`): `directory`, `personal`, `statutory`
or `banking`. `compensation` and `sensitive` are declared now for documents (contracts,
medical notes); payroll and discipline add fields later. Reading a class needs
`hr.employee.<class>.read` on that person (`hr.employee.read` for directory), changing
it needs the matching `.update`. Services return only the readable classes, so hidden
values never reach templates, exports or the audit log (masked classes).

**Encryption at rest.** Statutory identifiers (NIN, TIN, RSA PIN, NHF number) and banking
identifiers (account number, BVN) are encrypted with the Fernet key from ADR 0011. They
are never searched or sorted. Bank name, account name and PFA name stay plain.

**Who sees what (role templates, editable copies).**
- Employee: every colleague's directory fields; all of their own classes; may change
  their own contact details and next of kin directly, and request statutory or banking
  changes.
- Line Manager: personal details of their team (confirm).
- HR Officer: directory, personal, statutory and banking for everyone; not compensation
  or sensitive (confirmed 2026-11-19).
- HR Manager: every HR permission.
- IT Support (new template): completes IT checklist tasks.

**Nobody changes their own HR-managed fields.** Employees edit only the self-service
fields of their own record (contact details, address, state of residence, marital
status, next of kin), even if a role would let them edit others. Their names, dates,
employment details and identifiers are changed by someone else.

**Statutory and banking changes from employees** become change requests. The new values
are encrypted in the request. Approving one needs the update permission of that class,
and nobody approves their own request. Personal changes apply at once and are audited
(confirmed 2026-11-19).

**Employee numbers** are gap-free, allocated from a locked counter row in the same
transaction as the employee (`EMP-00001`; the prefix and padding are editable).

**Employee documents** go through the files service. Categories (editable) carry a field
class; there is one file purpose per class (`employee_document_<class>`), whose attach
and view permissions are that class's update and read permissions on the employee.

**Onboarding and offboarding checklists.** Templates are per department or position, or
the default; the most specific active template wins. Each task is for HR, IT, the
manager (from the org resolver on the day the checklist starts) or the employee, with a
due date relative to the start (hire date or last working day). HR completes any task;
IT tasks need `hr.checklist.it`; the manager and the employee complete their own.

**Invitation.** HR invites the employee from their record (`identity.user.invite`). The
new account is linked to the employee and given the role copied from the Employee
template. This one assignment skips "can't grant what you don't hold" because HR can't
hold every self-scoped permission; it is refused if that role has been edited to include
any permission wider than `self` other than the directory read.

**Exits and automatic revocation.** An exit records resignation or termination, reason,
notice date, last working day and a final-pay flag, and starts the offboarding
checklist. A beat task every 15 minutes revokes access for exits whose last working day
has ended (midnight Lagos time after it, confirmed 2026-11-19): the account is
deactivated, sessions, API keys, delegations and role assignments end, positions end on
the last working day and the record becomes `terminated`. An exit whose last day has
already passed is revoked when recorded. Exits can be cancelled until revoked. The last
active Super Administrator can't be given an exit.

**Background tasks.** Celery's autodiscovery only looks for `app.tasks`, so
`app/worker.py` imports each package's task module (files and HR). This also makes the
Phase 5 scan and retention schedules run.

## Consequences

Payroll (Phase 9) reads statutory and banking values through the HR service, which
decrypts them for permitted callers only. Lifecycle changes will reuse the change-request
pattern.
