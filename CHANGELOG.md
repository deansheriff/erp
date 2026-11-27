# Changelog

All notable changes are recorded here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added — Phase 11e: Support
- Support teams, with a lead and agents, and categories routed to a team (ADR 0032).
- Tickets (`TKT-`):
  - raised by employees for themselves, or recorded by agents for a finance customer;
  - landing in the team's queue, where agents take them or the lead assigns them;
  - public replies (emailed to the requester or the customer's contact) and internal
    notes, plus file attachments;
  - waiting on the requester, resolution, a 1–5 rating, reopening, and closing
    automatically after 3 business days.
- SLA targets by priority in business hours (Mon–Fri 08:00–17:00 Lagos, skipping public
  holidays), all to confirm. Waiting time pauses the resolution clock.
- Every 15 minutes, breaches are marked, the assignee is warned at 75% and the team lead
  is emailed on a breach.
- An SLA report by team, priority and agent.
- Permissions `support.ticket.create`, `.read`, `.work`, `support.report.read` and
  `support.settings.manage`; grants for the Support Agent and employees.

### Added — Phase 11d: Projects
- Projects (`PRJ-`) with a client, manager, dates, budget by category (labour,
  materials, expenses, other), members, milestones and tasks; planned → active → on
  hold → completed or cancelled (ADR 0031). A new Project Manager role sets them up.
- A project on journal lines, like a cost centre. It can be named on:
  - invoice, credit note, purchase order, direct bill and expense claim lines;
  - stock issues;
  - manual journals.
- Weekly timesheets, approved by the project's manager, or by their line manager for
  the manager's own time.
- Hourly cost rates for labour cost; it isn't posted.
- Profitability from the ledger plus labour, budget against actual by category, and
  daily budget alerts at 80% and 100% (to confirm).
- Permissions `projects.project.create`, `.read`, `projects.time.log`, `.approve`,
  `.approve_any`, `projects.rate.manage` and `projects.settings.manage`.

### Added — Phase 11c: Procurement
- Purchase requisitions (`PR-`) from any employee, for stock items or described goods and
  services, with estimated prices (ADR 0030).
- Approval by amount in a chain: the line manager, then a Finance Manager over ₦500,000,
  then a Director over ₦5,000,000 (all to confirm, editable in Procurement settings).
  - Each step is decided by a different person, never the requester.
  - The new Director role steps in when there's no line manager.
- Requests for quotation (`RFQ-`):
  - emailed to suppliers with the RFQ as a PDF attachment;
  - quotes recorded per line, with the supplier's document attached;
  - a comparison of the quotes line by line.
- Awards and orders:
  - Three quotes are needed over ₦1,000,000 (to confirm).
  - Awarding a quote that isn't the lowest needs a reason.
  - An award over the approved tier goes back for the missing approvals.
  - The award becomes a draft purchase order that someone else approves.
- Supplier performance: on-time delivery, lines received in full, returns, bills within
  the price tolerance, quotes won, and a 1–5 rating per order.
- Outgoing mail can carry one attachment.
- New permissions:
  - `procurement.requisition.*` (create, read, approve, approve_any, approve_finance and
    approve_director);
  - `procurement.rfq.read` and `.manage`;
  - `procurement.supplier.rate`;
  - `procurement.settings.manage`.
- Grants for the Procurement Officer, Finance Managers, line managers and employees.

### Added — Phase 11b: Stock in purchasing and sales
- Purchase order and sales lines can name a stock item and warehouse (ADR 0029). A stock
  PO line is charged to the item's inventory account.
- Goods received notes bring stock in at the order price, with bins, lots and expiry or
  serial numbers, and post debit inventory, credit 2030 Goods received not invoiced (new,
  to confirm, a finance setting).
- Bills clear goods received not invoiced at the order price, with any price difference
  to the item's variance account; debit notes send stock back to the supplier at cost.
- Invoices take stock out at cost and post cost of sales in the same journal, refused
  without enough on hand; credit notes put it back at the invoice's cost.
- Stock move kinds `receipt`, `purchase_return`, `sale` and `sale_return`.

### Added — Phase 11a: Inventory
- Items with a unit of measure, category, FIFO or weighted-average valuation, lot or
  serial tracking and reorder levels; warehouses and bins; item categories naming the
  inventory, cost of sales, adjustment and issue accounts (ADR 0028).
- Issues to a cost centre and transfers, posted by inventory staff; adjustments (also for
  opening stock) posted by a second person. Stock never goes negative; lot items without
  a lot take the earliest-expiring lots first.
- Stock counts with the variance posted by a second person.
- Material requests from any employee, approved by their line manager and issued in
  parts.
- A daily low-stock email to people who manage items, at most once a day per item and
  warehouse.
- Stock journals (source `stock`) for issues, adjustments and count variances; a new
  5200 Stock adjustments and variances account (to confirm).
- Permissions `inventory.item.*`, `inventory.stock.*` and `inventory.request.*`; a new
  Inventory Manager role, and grants for the Inventory Clerk, line managers and
  employees.

### Fixed
- The worker now loads finance's scheduled tasks (recurring journals, payment reminders
  and depreciation drafts), which it hadn't been registering.

### Added — Phase 10e: Fixed assets
- Asset categories with a depreciation method (straight line, reducing balance or none),
  useful life or rate and their accounts; new accounts 1511 to 1551, 3300, 4910 and 6910
  and a ₦100,000 capitalisation threshold, all to confirm (ADR 0027).
- The register (`FA-`): assets from posted bill lines on a cost account, or by hand with
  depreciation from before go-live, and a comparison with the ledger.
- Monthly depreciation runs, prepared nightly or by hand and posted by someone else, with
  a full month in the month acquired and catch-up for late registrations.
- Revaluations to a reserve and disposals with a gain or loss, each approved by a second
  person.
- Permissions `finance.asset.read`, `.manage` and `.approve`; grants for the Finance
  Officer and Finance Manager.

### Added — Phase 10d: Banking
- Bank accounts, each with its own ledger account (1010 first, new ones from 1011), bank
  details and a statement layout (ADR 0026).
- Statement import from CSV, Excel or OFX; files kept 6 years (to confirm); lines already
  imported are skipped.
- Auto-matching on the exact amount within 3 days (editable), rules that give the rest an
  entry account, and matching by hand.
- Reconciliations to a statement date, down to a nil difference, submitted by one person
  and approved by another; approval posts the rule and hand entries and locks the lines.
- Transfers between bank accounts (`TRF-`) approved by a second person.
- Marking a tax return paid now posts the payment from a chosen bank account.
- Permissions `finance.bank.read`, `.manage`, `.reconcile` and `.approve`; grants for the
  Finance Officer and Finance Manager.

### Added — Phase 10c: Payables
- Suppliers (company or individual, for the WHT rate) with terms, a usual WHT type and
  bank details for the payment file (ADR 0025).
- Purchase orders approved by someone other than the drafter, and goods received against
  their lines (nothing posts). Gap-free numbers `PO-`, `GRN-`.
- Bills (`BILL-`) from a purchase order or direct, approved by a second person, with a
  three-way match on approval: billed quantities against received, prices within a 2%
  tolerance (to confirm). Bills that fail are held until a Finance Manager posts them
  with a reason. Supplier invoice numbers can't be entered twice.
- Debit notes (`DN-`) against a posted bill, issued by a second person.
- Supplier payments (`PAY-`) with WHT withheld at payment at the supplier's company or
  individual rate, approved by a second person, downloadable as the generic bank CSV,
  and reversible with a reason. VAT input and WHT payable now reach the tax returns.
- Payables aging with CSV; supplier pages with what is owed and open purchase orders.
- Permissions `finance.supplier.*`, `finance.purchase.*`, `finance.bill.override` and
  `finance.payment.*`; grants for the Finance Officer, Finance Manager and Auditor.

### Added — Phase 10b: Receivables and tax
- Customers with payment terms (30 days by default, to confirm), TIN and reminders on or
  off (ADR 0024).
- Quotes, sales orders, invoices and credit notes, every step optional; converting copies
  the lines into a new draft. Gap-free numbers `QT-`, `SO-`, `INV-`, `CN-`.
- VAT per line, standard (7.5% from 1 February 2020, to confirm), zero-rated or exempt,
  with prices excluding VAT; optional stamp duty types. Issuing an invoice or credit note
  posts its journal in the same transaction; credit notes are issued by someone other
  than their drafter.
- Receipts (`RCT-`) with cash and WHT deducted, allocated to invoices, credit left on
  account to allocate later, WHT credit notes recorded, and reversal with a reason.
- Aging (current to 90+ days) with CSV, customer statements with PDF, PDFs of each
  document, emailing a document to the customer, and payment reminders emailed before,
  on and after the due date (once a day at most).
- Tax settings: effective-dated VAT rates, withholding-tax types (seeded without rates),
  stamp duty types, and the receivables accounts, terms and reminder days.
- Monthly VAT, WHT and stamp duty returns read from the ledger, due the 21st (to
  confirm), filed and paid with references, flagged if the ledger moves after filing.
- Permissions `finance.customer.*`, `finance.sales.*`, `finance.credit_note.issue`,
  `finance.receipt.*` and `finance.tax.*`; grants for the Finance Officer, Finance Manager
  and Auditor.

### Added — Phase 10a: General ledger
- Chart of accounts: hierarchical, typed asset, liability, equity, income or expense, with
  header accounts and a cash-flow class on balance-sheet accounts. A Nigerian SME chart is
  seeded using the codes payroll and expenses already use (to confirm) (ADR 0023).
- Financial years January to December with twelve months; a Finance Manager closes
  months (not while drafts are dated in them), reopens one with a reason, and closes the
  year to retained earnings for good.
- Journals: drafted by one person and posted by another; at least two lines that balance,
  active accounts, an open month; gap-free numbers `JV-00001`; posted journals reversed,
  never edited. Live debit and credit totals while editing.
- Payroll and expenses now post their journals into the ledger in the same transaction.
  Journals dated in a closed or unopened month, or using an unknown account, are held for
  Finance to post or re-date with a reason. Payroll remittances and expense payments show
  each journal's ledger number or that it is held.
- Recurring journals drafted on their day each month by a daily task; someone other than
  the template's author posts them.
- Reports: trial balance, profit and loss (by cost centre), balance sheet, cash flow
  (indirect) and the general ledger, with CSV for the trial balance and ledger.
- Permissions `finance.account.*`, `finance.journal.*`, `finance.period.manage` and
  `finance.report.read`; grants for the Finance Officer, Finance Manager and Auditor.

### Added — Phase 9b: Payroll remittances, loans and tax certificates
- Remittances from an approved run as CSV: the bank payment file (CBN bank codes, with
  anyone who can't be paid listed), PAYE per state of residence, pension per PFA and
  NHF (ADR 0022). Downloads need `payroll.remittance.export` and are audited.
- Bank codes for the common Nigerian banks, matched to the bank named on HR records,
  and ledger account codes, both editable under Payroll settings (to confirm).
- Journals queued for the ledger: the payroll accrual when a run is posted (costs by
  cost centre), net pay when it is paid, and disbursement when a loan is approved.
- Interest-free staff loans and salary advances: recorded by Payroll, approved by
  Finance (not the recorder or the borrower), repaid by monthly payroll deductions
  until cleared, with a repayment schedule. Employees see theirs on My payslips.
- Year-end tax certificates (PDF) by month from approved runs; employees download their
  own once December is paid.
- Permissions `payroll.remittance.export`, `payroll.loan.read`, `payroll.loan.manage`,
  `payroll.loan.approve` and `payroll.certificate.read`; loan numbers `LN-00001`.

### Added — Phase 9a: Payroll runs and payslips
- Pay components (earnings and deductions; a fixed amount per person or a percentage
  of another earning; taxable and pensionable flags), salary structures that group
  them, and monthly pay groups (ADR 0021). Basic, housing, transport, meal,
  cooperative and union dues are seeded with a Standard structure and a Staff group.
- Pay per person, effective-dated with a reason; changes end the previous record.
  Nobody changes their own pay. Recurring deductions by month, with an end month.
- Statutory rates as effective-dated sets, editable by the Finance Manager: PAYE bands
  (NTA 2025 defaults), rent relief 20% capped at ₦500,000, pension 8% and 10%, NHF
  2.5%, NHIS, NSITF 1%, ITF 1%, a minimum wage warning at ₦70,000 and overtime at 1.5x
  hourly basic over 173 hours. All to confirm before go-live.
- PAYE worked out cumulatively over the year to date. Pay prorated by calendar day for
  joiners and leavers; approved attendance overtime paid. Warnings for pay under the
  minimum wage, negative net pay, missing basic and earlier unapproved runs.
- Runs go draft, calculated, reviewed, approved, posted, paid, each step by a different
  person; whoever created the run can't post it or mark it paid. Runs can be sent back
  to draft with a note. Payslip PDFs are made at approval and employees see their
  payslips once the run is paid.
- Rent relief: employees declare the year's rent with proof; HR or Payroll verifies it
  (not their own) before it reduces PAYE.
- Permissions `payroll.pay.*`, `payroll.structure.manage`, `payroll.settings.manage`,
  `payroll.run.*`, `payroll.payslip.read` and `payroll.relief.*`; grants for the
  Employee, HR Officer, Payroll Officer, Finance Officer, Finance Manager and Auditor
  templates.

### Added — Phase 8: Expenses
- Expense categories with a ledger account code, an optional limit per claim and a
  "receipt needed above" amount; claims with dated lines, receipts through the files
  service (`expense_receipt`) and cost-centre tags (ADR 0020). Naira only for now.
- Approval chain from the org chart, tiered by amount: line manager, then the next
  manager up above ₦100,000, then Finance above ₦500,000 (to confirm). Named approvers,
  their delegates, or organisation-wide approvers decide; nobody approves their own.
  Approvers can correct line amounts (the original is kept and logged), return a claim
  for changes, or reject it.
- Over a category limit or a weekly or monthly spending limit (defaults ₦50,000 and
  ₦150,000, per-person overrides), claims still go but approvers see a warning.
- Payments by the Reimburser, several at once, with date, method and reference; the
  approver can't pay a claim they approved without the override permission.
- Cash advances: approved and paid like claims, used up first by later claims, and the
  rest repaid and recorded by Finance.
- Every payment and repayment queues a balanced journal for the ledger (Phase 10).
- Permissions `expenses.claim.*` and `expenses.settings.manage`; grants for the
  Employee, Line Manager, Expense Approver, Reimburser, Finance Officer, Finance
  Manager and Auditor templates. Numbers `EXP-00001` and `ADV-00001`.

### Added — Phase 7b: Attendance and shifts
- Shift patterns (start, end — past midnight allowed — break, working days, late and
  early grace, 15 minutes each by default, to confirm), usual shifts per person with
  dates, and single-day roster changes (another shift or a day off) (ADR 0019). A Day
  shift (Monday to Friday, 08:00 to 17:00, an hour's break) is seeded.
- Clock in and out from the browser, mobile first. Per branch: a geofence (clock-in is
  refused outside the radius or without a location) and an optional photo at clock-in,
  kept 90 days (to confirm) and seen only by the person and HR.
- A day's status worked out from the shift, the clock times, approved leave and public
  holidays: absent, late, left early, missing clock-out (four hours after the shift,
  to confirm; no hours count until corrected), on leave, holiday. Only people with a
  shift are judged.
- Overtime is time past the shift's end, recorded at clock-out and counted only once a
  manager (or HR) approves it. Corrections to a day's times go to the same approvers;
  nobody approves their own.
- Clock in, timesheet with corrections, Team today with approvals, monthly summary with
  a CSV for payroll, Shifts & roster, and settings (patterns and branch locations).
- Files can expire a set time after upload (`FilePurpose.expires_after`).
- Permissions `attendance.clock.use`, `attendance.record.read|approve`,
  `attendance.correction.request`, `attendance.photo.view`,
  `attendance.schedule.manage`, `attendance.settings.manage`; grants for the Employee,
  Line Manager, HR Officer, HR Manager and Auditor templates.

### Added — Phase 7a: Leave
- Leave types with entitlement rules by grade and length of service, yearly or monthly
  accrual, carry-over with a cap and expiry, optional HR final approval and a medical
  certificate threshold (ADR 0018). Defaults to confirm: annual 6 working days after
  12 months, sick 12 working days (certificate over 2 days), maternity 84 calendar days;
  paternity, compassionate and study start at zero; unpaid has no balance.
- Working days skip weekends and an admin-managed list of public holidays (fixed-date
  and Easter holidays seeded for 2026 and 2027); half days at the start or end.
- Requests refused beyond the available balance; manager approval from the org chart,
  then HR when the type requires it (HR can always step in); nobody approves their own
  leave; certificates stored as `sensitive` files.
- My leave (balances and requests), request form with a day check, approvals inbox with
  who else is away, team calendar, balances with audited adjustments, and settings.
- Carry-over job (daily, writes each new year's carry-over once).
- Permissions `leave.request.*`, `leave.balance.*`, `leave.settings.manage`; grants for
  the Employee, Line Manager, HR Officer, HR Manager and Auditor templates.

### Added — Phase 6: HR core and self-service
- The HR record (ADR 0017): personal, employment, next-of-kin, statutory and bank
  fields, each in a field class. Every screen, list and audit entry gets only the
  classes the viewer may read; NIN, TIN, RSA PIN, NHF number, account number and BVN
  are encrypted at rest. Nobody changes their own HR-managed fields.
- Gap-free employee numbers (`EMP-00001`, prefix and digits editable), allocated in the
  same transaction as the person.
- Self-service (`/me`): own details, documents, requests and checklist tasks; contact
  and next-of-kin changes save at once; statutory and bank changes go to HR as change
  requests (HR → Change requests), which nobody may approve for themselves.
- Colleague directory (`/directory`): directory fields only.
- Employee documents through the files service, one purpose per field class, with
  editable categories.
- Onboarding and offboarding checklists from templates per position, department or
  default; tasks for HR, IT, the manager or the employee (HR → Joiners & leavers).
- Inviting an employee from their profile links the new account and gives the Employee
  role. Exits (resignation or termination) start offboarding; access is revoked
  automatically at midnight Lagos time after the last working day (account, sessions,
  API keys, delegations, roles and positions).
- HR settings: employee numbers, document categories, checklist templates.
- Permissions per field class (`hr.employee.<class>.read/update`),
  `hr.profile.request_change`, `hr.checklist.manage`, `hr.checklist.it`,
  `hr.exit.manage`, `hr.settings.manage`; grants for the Employee, Line Manager,
  HR Officer and HR Manager templates and a new IT Support template.

### Changed
- HR → People opens a full profile page; adding and editing people use a full-page
  form instead of the drawer.

### Fixed
- The Celery worker now imports each package's tasks, so the file scan and retention
  schedules (Phase 5) and the access-revocation schedule actually run.

### Added — Phase 5: Files
- One file service (ADR 0016): random storage keys in private quarantine and clean
  buckets, a purpose registry (types, size, attach and view permissions, retention,
  inline display, classification), and `GET /files/{id}` authorised against the entity
  the file belongs to. Downloads carry the detected type, `nosniff`, and
  `attachment` unless the purpose allows inline display; sensitive classes are audited.
- Uploads are read as they stream in, with the size limit enforced on the way and
  nothing written to disk; types come from the content (magic bytes); SVGs are refused
  if they carry scripts, event handlers, embedded HTML or external links, and are served
  under a sandboxing content policy.
- Virus scanning through ClamAV (INSTREAM). Clean files move to the clean bucket;
  infected ones stay quarantined, are audited and reported to the uploader and file
  managers by email. An unreachable scanner leaves files pending (fail closed).
- Versions, soft delete, a nightly retention job, legal hold, and a 50 GB organisation
  quota tracked on upload and purge.
- The organisation logo (PNG, JPEG, WebP or sanitised SVG, up to 2 MB) on the
  Organisation page and in the sidebar; Administration → Files with usage, scan states,
  details, legal hold, deletion and the quota.
- Permissions `files.file.manage` (sensitive) and `system.storage.manage`.

### Changed
- Multipart requests must send the CSRF token as the `X-CSRF-Token` header (HTMX does),
  so uploads are never parsed before the file service can enforce its limits.

### Added — Phase 4: Organisation structure
- Branches, cost centres, grades, nestable departments, positions (a reporting tree) and
  position assignments (primary, acting or interim, with dates), plus a minimal employee
  record in the new HR module (ADR 0015).
- The org resolver: who holds a position, a person's manager, their direct and indirect
  reports, the approval chain and their departments, as of any date. Vacant positions are
  skipped, a primary holder always wins, and cycles in bad data stop the walk and are
  logged. Moving a department or position under its own descendant is refused.
- Team and department scopes now come from the position tree, including for records
  without a department of their own (employees).
- Screens: Org chart, Position detail, Departments, Structure setup and People under HR;
  Organisation (legal details and document colours) under Administration.
- Permissions `org.structure.read`, `org.structure.manage`, `org.assignment.manage`
  (sensitive), `hr.employee.read`, `hr.employee.create` and `hr.employee.update`.

### Changed
- The worker starts with `celery -A app.worker`; `app/wiring.py` connects the core to
  business modules, and an import contract stops core packages importing `app.modules`.

### Added — Phase 3: Authorisation
- One policy engine (ADR 0013): permission catalogue declared in each package's
  `policies.py`, scopes (self, team, department, organisation), `check()` with a reason and
  `scope_filter()` for SQL lists. Grants are read live per request, cached under an
  authorisation version and the next time-based change.
- Role templates (16) copied into editable roles; protected Super Administrator (ADR 0014),
  Organisation Administrator (everything except technical settings) and Auditor (reads
  everything). Two-step verification can be required per role.
- Role assignments with optional end dates (acting roles), department restriction and a
  required reason; delegations of up to 90 days, evaluated from the delegator's position.
- Nobody can grant what they don't hold, change their own roles, remove the last super
  admin, or delegate access-control and technical permissions.
- Field classes with redaction that hides unclassified fields; separation-of-duties rules
  as data.
- Screens: Roles, role editor with a permission matrix, a person's access page, my
  delegations, everyone's delegations and the audit log viewer. The navigation shows only
  what the viewer may use, and every refused route is audited.
- `docs/PERMISSIONS.md` and a reviewed template snapshot, both generated from code and
  checked in CI. The app refuses to start if code and database permissions differ.

### Changed
- `users.is_super_admin` replaced by the Super Administrator role (migration 0003).

### Added — Phase 2: Identity, installation and mail
- First-run setup wizard (organisation → super admin with mandatory two-step verification →
  SMTP), protected by a one-time setup code printed to the log or issued by `kx setup-token`.
- Sign-in with Argon2id passwords, lockout after 5 failures for 15 minutes, per-IP rate
  limit, TOTP two-step verification with replay protection and ten one-time recovery codes.
- Server-side sessions (30-minute idle, 12-hour absolute) with synchronizer-token CSRF and
  Origin checks; users can see and sign out their devices.
- Invitations (72-hour single-use links) and password resets (30-minute single-use links)
  sent through a transactional mail outbox and a Celery worker; admins never see passwords.
- API keys: shown once, stored hashed, expiring and revocable; accepted on `/api/*` only.
- Administration: users list with invite, resend, suspend (ends sessions and keys),
  reactivate and unlock; SMTP settings with an encrypted password and a test email.
- Central access guard enforcing every route's declaration; interim super-admin rule until
  the Phase 3 policy engine (ADR 0010, since superseded by ADR 0014).
- ADRs 0009–0012; docs/IDENTITY.md.

### Fixed
- The CI route-declaration and screen-registry checks found no routes under FastAPI 0.163
  (routers are nested), so they passed without checking anything. They now walk every
  router explicitly and fail if fewer than 20 routes are found.

### Added — Phase 1: Foundation
- Repository skeleton, uv-managed Python 3.12 project, ruff, strict mypy, import-linter,
  pre-commit, GitHub Actions CI with a real PostgreSQL 16 service.
- docker-compose with Postgres, Redis, MinIO (quarantine and clean buckets), ClamAV,
  Mailpit, a one-shot migration job, the app and a Celery worker.
- Settings from `KX_*` environment variables; JSON logging with request IDs.
- Separate database roles (`kx_owner`, `kx_migrator`, `kx_app`) with a start-up check that
  refuses to run as a privileged role or table owner.
- Append-only `audit_log`, with masked diffs for sensitive fields and independent writes
  for denied actions.
- Domain errors with user-safe messages; generic 500 page with a reference ID; HTMX error
  toasts; strict Content-Security-Policy and security headers.
- "Chroma" design system in Google Stitch; app shell (desktop and mobile), shared
  components and system pages generated in Stitch and implemented as Jinja templates
  with Tailwind, HTMX and Alpine (CSP build). Light and dark modes, module hues and
  subtle, reduced-motion-aware animation.
- CI rules: every route declares a permission or `@public`; no async routes; every HTML
  route is in `screens.yaml` with its Stitch export; no `session.query()`; no exception
  text in responses; storage and disk writes only in `app/files`; modules import each
  other only through `service.py`.

### Changed
- One installation per organisation instead of multi-tenancy (ADR 0002).
