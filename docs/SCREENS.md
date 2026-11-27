# Screens

<!-- Generated from design/stitch/screens.yaml by scripts/gen_screens_doc.py. Do not edit by hand. -->

Stitch project `2736752339961073788`, design system `assets/5eab0debd3b04eb5ac88fc259fd666ca`.

## Phase 1

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **App shell (sidebar, top bar, mobile bottom navigation)** (`core.shell`) | `GET /_dev/shell` | public (development preview only) | directory | Dean Sheriff (2026-11-17) |
| **Shared components gallery** (`core.components`) | `GET /_dev/components` | public (development preview only) | — | Dean Sheriff (2026-11-17) |
| **No access (403), not found (404) and server error (500) pages** (`core.system_pages`) | `GET /_dev/errors/{kind}` | public | — | Dean Sheriff (2026-11-17) |

## Phase 2

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **Signed-in home** (`core.home`) | `GET /` | signed in | directory | Dean Sheriff (2026-11-18) |
| **First-run setup wizard (organisation, super admin, mail)** (`setup.wizard`) | `GET /setup` | public until installed, then closed (setup code required) | directory | Dean Sheriff (2026-11-18) |
| **Sign in** (`identity.login`) | `GET /login` | public | — | Dean Sheriff (2026-11-18) |
| **Two-step verification code and recovery code** (`identity.mfa_verify`) | `GET /login/verify` | signed in, awaiting two-step verification | — | Dean Sheriff (2026-11-18) |
| **Set up two-step verification and recovery codes** (`identity.mfa_enrol`) | `GET /login/two-step` | signed in, own account | — | Dean Sheriff (2026-11-18) |
| **Forgot password and choose a new password** (`identity.password_reset`) | `GET /forgot-password` | public (single-use emailed link) | — | Dean Sheriff (2026-11-18) |
| **Accept invitation and create account** (`identity.invite_accept`) | `GET /invite/{token}` | public (single-use emailed link) | directory | Dean Sheriff (2026-11-18) |
| **My security (password, two-step, devices, API keys)** (`identity.security`) | `GET /me/security` | signed in, own account | — | Dean Sheriff (2026-11-18) |
| **Users and invitations** (`admin.users`) | `GET /admin/users` | identity.user.read | directory | Dean Sheriff (2026-11-18) |
| **Mail (SMTP) settings** (`admin.mail`) | `GET /admin/mail` | system.mail.manage | — | Dean Sheriff (2026-11-18) |

## Phase 3

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **Roles** (`admin.roles`) | `GET /admin/roles` | authz.role.read | — | Dean Sheriff (2026-11-18) |
| **Role editor (permission matrix and scopes)** (`admin.role_editor`) | `GET /admin/roles/{role_id}` | authz.role.read | — | Dean Sheriff (2026-11-18) |
| **A person's access (roles, delegations, effective permissions)** (`admin.user_access`) | `GET /admin/users/{user_id}/access` | identity.user.read | directory | Dean Sheriff (2026-11-18) |
| **My delegations** (`delegations`) | `GET /delegations` | signed in, own delegations | directory | Dean Sheriff (2026-11-18) |
| **Everyone's delegations** (`admin.delegations`) | `GET /admin/delegations` | authz.delegation.manage | directory | Dean Sheriff (2026-11-18) |
| **Audit log viewer** (`admin.audit`) | `GET /admin/audit` | audit.log.read | — | Dean Sheriff (2026-11-18) |

## Phase 4

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **Org chart** (`hr.org_chart`) | `GET /hr/org-chart` | org.structure.read | directory | Dean Sheriff (2026-11-19) |
| **Position detail (holders, reports, reporting line)** (`hr.position`) | `GET /hr/positions/{position_id}` | org.structure.read | directory | Dean Sheriff (2026-11-19) |
| **Departments** (`hr.departments`) | `GET /hr/departments` | org.structure.read | — | Dean Sheriff (2026-11-19) |
| **Structure setup (branches, cost centres, grades)** (`hr.structure`) | `GET /hr/structure` | org.structure.read | — | Dean Sheriff (2026-11-19) |
| **People (directory)** (`hr.people`) | `GET /hr/people` | hr.employee.read | directory | Dean Sheriff (2026-11-19) |
| **Organisation details and document colours** (`admin.organisation`) | `GET /admin/organisation` | system.branding.manage | — | Dean Sheriff (2026-11-19) |

## Phase 5

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **Files and storage** (`admin.files`) | `GET /admin/files` | files.file.manage | — | Dean Sheriff (2026-11-19) |

## Phase 6

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **Employee profile** (`hr.employee`) | `GET /hr/people/{id}` | hr.employee.read | directory, personal, statutory, banking, compensation, sensitive | Dean Sheriff (2026-11-19) |
| **Add or edit a person** (`hr.employee_form`) | `GET /hr/people/new, GET /hr/people/{id}/edit` | hr.employee.create | directory, personal, statutory, banking | Dean Sheriff (2026-11-19) |
| **Change requests** (`hr.change_requests`) | `GET /hr/change-requests` | hr.employee.statutory.update | statutory, banking | Dean Sheriff (2026-11-19) |
| **Joiners and leavers** (`hr.checklists`) | `GET /hr/checklists` | login | directory | Dean Sheriff (2026-11-19) |
| **HR settings** (`hr.settings`) | `GET /hr/settings` | hr.settings.manage | — | Dean Sheriff (2026-11-19) |
| **Colleague directory** (`hr.directory`) | `GET /directory` | hr.employee.read | directory | Dean Sheriff (2026-11-19) |
| **My profile (self-service)** (`me.profile`) | `GET /me` | login | directory, personal, statutory, banking, compensation, sensitive | Dean Sheriff (2026-11-19) |

## Phase 7

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **My leave (self-service)** (`leave.my`) | `GET /leave` | leave.request.read | directory, sensitive | Dean Sheriff (2026-11-19) |
| **Request leave** (`leave.request`) | `GET /leave/new` | leave.request.create | directory | Dean Sheriff (2026-11-19) |
| **Leave approvals** (`leave.approvals`) | `GET /leave/approvals` | login | directory | Dean Sheriff (2026-11-19) |
| **Team leave calendar** (`leave.calendar`) | `GET /leave/calendar` | leave.request.read | directory | Dean Sheriff (2026-11-19) |
| **Leave balances** (`leave.balances`) | `GET /leave/balances` | leave.balance.read | directory | Dean Sheriff (2026-11-19) |
| **Leave settings** (`leave.settings`) | `GET /leave/settings` | leave.settings.manage | — | Dean Sheriff (2026-11-19) |
| **Clock in (self-service)** (`attendance.clock`) | `GET /attendance` | attendance.clock.use | directory, sensitive | Dean Sheriff (2026-11-20) |
| **Timesheet and corrections** (`attendance.timesheet`) | `GET /attendance/timesheet` | attendance.record.read | directory, sensitive | Dean Sheriff (2026-11-20) |
| **Team today** (`attendance.team`) | `GET /attendance/team` | attendance.record.read | directory, sensitive | Dean Sheriff (2026-11-20) |
| **Monthly attendance summary** (`attendance.summary`) | `GET /attendance/summary` | attendance.record.read | directory | Dean Sheriff (2026-11-20) |
| **Shifts and roster** (`attendance.roster`) | `GET /attendance/roster` | attendance.schedule.manage | directory | Dean Sheriff (2026-11-20) |
| **Attendance settings** (`attendance.settings`) | `GET /attendance/settings` | attendance.settings.manage | — | Dean Sheriff (2026-11-20) |

## Phase 8

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **My expenses (self-service)** (`expenses.my`) | `GET /expenses` | expenses.claim.create | directory | Dean Sheriff (2026-11-20) |
| **Expense claim or cash advance** (`expenses.document`) | `GET /expenses/{document_id}` | login | directory | Dean Sheriff (2026-11-20) |
| **Expense approvals** (`expenses.approvals`) | `GET /expenses/approvals` | login | directory | Dean Sheriff (2026-11-20) |
| **Expense payments** (`expenses.payments`) | `GET /expenses/payments` | expenses.claim.reimburse | directory | Dean Sheriff (2026-11-20) |
| **Expense settings** (`expenses.settings`) | `GET /expenses/settings` | expenses.settings.manage | — | Dean Sheriff (2026-11-20) |

## Phase 9

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **Payroll runs** (`payroll.runs`) | `GET /payroll` | payroll.run.read | directory, compensation | Dean Sheriff (2026-11-20) |
| **Payroll run** (`payroll.run`) | `GET /payroll/runs/{run_id}` | payroll.run.read | directory, compensation | Dean Sheriff (2026-11-20) |
| **My payslips (self-service)** (`payroll.my`) | `GET /payslips` | payroll.payslip.read | compensation | Dean Sheriff (2026-11-20) |
| **People's pay** (`payroll.people`) | `GET /payroll/people` | payroll.pay.read | directory, compensation | Dean Sheriff (2026-11-20) |
| **Salary structures, components and pay groups** (`payroll.structures`) | `GET /payroll/structures` | payroll.structure.manage | — | Dean Sheriff (2026-11-20) |
| **Rent relief declarations** (`payroll.reliefs`) | `GET /payroll/reliefs` | payroll.relief.verify | directory, compensation | Dean Sheriff (2026-11-20) |
| **Payroll settings (statutory rates)** (`payroll.settings`) | `GET /payroll/settings` | payroll.settings.manage | — | Dean Sheriff (2026-11-20) |
| **Payroll remittances and files** (`payroll.remittances`) | `GET /payroll/remittances` | payroll.run.read | statutory, banking, compensation | Dean Sheriff (2026-11-20) |
| **Loans and salary advances** (`payroll.loans`) | `GET /payroll/loans` | payroll.loan.read | directory, compensation | Dean Sheriff (2026-11-20) |
| **Year-end tax certificates** (`payroll.certificates`) | `GET /payroll/certificates` | payroll.certificate.read | directory, statutory, compensation | Dean Sheriff (2026-11-20) |

## Phase 10

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **Journals** (`finance.journals`) | `GET /finance/journals` | finance.journal.read | — | Dean Sheriff (2026-11-22) |
| **Journal (draft editor, held or posted)** (`finance.journal`) | `GET /finance/journals/{journal_id}` | finance.journal.read | — | Dean Sheriff (2026-11-22) |
| **Chart of accounts** (`finance.accounts`) | `GET /finance/accounts` | finance.account.read | — | Dean Sheriff (2026-11-22) |
| **Recurring journals** (`finance.recurring`) | `GET /finance/recurring` | finance.journal.read | — | Dean Sheriff (2026-11-22) |
| **Financial years and periods** (`finance.periods`) | `GET /finance/periods` | finance.journal.read | — | Dean Sheriff (2026-11-22) |
| **Financial reports** (`finance.reports`) | `GET /finance/reports` | finance.report.read | — | Dean Sheriff (2026-11-22) |
| **Customers** (`finance.customers`) | `GET /finance/customers` | finance.customer.read | — | Dean Sheriff (2026-11-22) |
| **One customer (balance, aging, statement, credit, reminders)** (`finance.customer`) | `GET /finance/customers/{customer_id}` | finance.customer.read | — | Dean Sheriff (2026-11-22) |
| **Sales (invoices, quotes, orders, credit notes, receipts)** (`finance.sales`) | `GET /finance/sales` | finance.sales.read | — | Dean Sheriff (2026-11-22) |
| **Quote, order, invoice or credit note** (`finance.sales_document`) | `GET /finance/sales/{document_id}` | finance.sales.read | — | Dean Sheriff (2026-11-26) |
| **Record or view a receipt** (`finance.receipt`) | `GET /finance/receipts/{receipt_id}` | finance.sales.read | — | Dean Sheriff (2026-11-22) |
| **Receivables aging** (`finance.aging`) | `GET /finance/aging` | finance.customer.read | — | Dean Sheriff (2026-11-22) |
| **Tax settings** (`finance.tax`) | `GET /finance/tax` | finance.tax.read | — | Dean Sheriff (2026-11-22) |
| **Tax returns** (`finance.tax_returns`) | `GET /finance/tax/returns` | finance.tax.read | — | Dean Sheriff (2026-11-22) |
| **Suppliers** (`finance.suppliers`) | `GET /finance/suppliers` | finance.supplier.read | — | Dean Sheriff (2026-11-22) |
| **One supplier (owed, aging, bills, orders, bank details)** (`finance.supplier`) | `GET /finance/suppliers/{supplier_id}` | finance.supplier.read | — | Dean Sheriff (2026-11-22) |
| **Purchases (bills, purchase orders, debit notes, payments)** (`finance.purchases`) | `GET /finance/purchases` | finance.purchase.read | — | Dean Sheriff (2026-11-22) |
| **Purchase order and goods received** (`finance.purchase_order`) | `GET /finance/purchase-orders/{order_id}` | finance.purchase.read | — | Dean Sheriff (2026-11-26) |
| **Supplier bill or debit note, with the three-way match** (`finance.bill`) | `GET /finance/bills/{bill_id}` | finance.purchase.read | — | Dean Sheriff (2026-11-26) |
| **Prepare or view a supplier payment** (`finance.payment`) | `GET /finance/payments/{payment_id}` | finance.purchase.read | — | Dean Sheriff (2026-11-22) |
| **Payables aging** (`finance.payables_aging`) | `GET /finance/payables-aging` | finance.supplier.read | — | Dean Sheriff (2026-11-22) |
| **Banking (bank accounts and matching rules)** (`finance.bank_accounts`) | `GET /finance/banking` | finance.bank.read | — | Dean Sheriff (2026-11-26) |
| **One bank account (statement import and matching)** (`finance.bank_account`) | `GET /finance/banking/{bank_id}` | finance.bank.read | — | Dean Sheriff (2026-11-26) |
| **Bank reconciliation** (`finance.reconciliation`) | `GET /finance/reconciliations/{rec_id}` | finance.bank.read | — | Dean Sheriff (2026-11-26) |
| **Transfers between bank accounts** (`finance.transfers`) | `GET /finance/transfers` | finance.bank.read | — | Dean Sheriff (2026-11-26) |
| **Fixed asset register (totals, assets from bills, ledger comparison)** (`finance.assets`) | `GET /finance/assets` | finance.asset.read | — | Dean Sheriff (2026-11-26) |
| **One fixed asset (figures, depreciation, revaluations and disposals)** (`finance.asset`) | `GET /finance/assets/{asset_id}` | finance.asset.read | — | Dean Sheriff (2026-11-26) |
| **Depreciation runs** (`finance.depreciation`) | `GET /finance/depreciation` | finance.asset.read | — | Dean Sheriff (2026-11-26) |
| **Asset categories and fixed asset settings** (`finance.asset_categories`) | `GET /finance/assets/categories` | finance.asset.read | — | Dean Sheriff (2026-11-26) |
| **Register or edit a fixed asset** (`finance.asset_form`) | `GET /finance/assets/new` | finance.asset.manage | — | Dean Sheriff (2026-11-26) |

## Phase 11

| Screen | Route | Permission | Field classes | Approved by |
|---|---|---|---|---|
| **Inventory items (stock value, low stock, the item list)** (`inventory.items`) | `GET /inventory/items` | inventory.item.read | — | Dean Sheriff (2026-11-26) |
| **One inventory item (stock by location, serials, stock ledger, details)** (`inventory.item`) | `GET /inventory/items/{item_id}` | inventory.item.read | — | Dean Sheriff (2026-11-26) |
| **Inventory setup (warehouses and bins, item categories, units)** (`inventory.setup`) | `GET /inventory/setup` | inventory.item.read | — | Dean Sheriff (2026-11-26) |
| **Stock movements and counts** (`inventory.movements`) | `GET /inventory/movements` | inventory.item.read | — | Dean Sheriff (2026-11-26) |
| **One issue, transfer or adjustment** (`inventory.movement`) | `GET /inventory/movements/{document_id}` | inventory.item.read | — | Dean Sheriff (2026-11-26) |
| **A stock count sheet** (`inventory.count`) | `GET /inventory/counts/{count_id}` | inventory.item.read | — | Dean Sheriff (2026-11-26) |
| **Material requests** (`inventory.requests`) | `GET /inventory/requests` | inventory.request.read | — | Dean Sheriff (2026-11-26) |
| **One material request (draft, decision, issue)** (`inventory.request`) | `GET /inventory/requests/{request_id}` | inventory.request.read | — | Dean Sheriff (2026-11-26) |
| **Purchase requisitions** (`procurement.requisitions`) | `GET /procurement/requisitions` | procurement.requisition.read | — | Dean Sheriff (2026-11-26) |
| **One purchase requisition (draft, approvals, starting the RFQ)** (`procurement.requisition`) | `GET /procurement/requisitions/{requisition_id}` | procurement.requisition.read | — | Dean Sheriff (2026-11-26) |
| **Requests for quotation** (`procurement.rfqs`) | `GET /procurement/rfqs` | procurement.rfq.read | — | Dean Sheriff (2026-11-26) |
| **One RFQ (suppliers, quote comparison, award and order)** (`procurement.rfq`) | `GET /procurement/rfqs/{rfq_id}` | procurement.rfq.read | — | Dean Sheriff (2026-11-26) |
| **Record a supplier quote** (`procurement.quote`) | `GET /procurement/rfqs/{rfq_id}/quotes/{supplier_id}` | procurement.rfq.manage | — | Dean Sheriff (2026-11-26) |
| **Supplier performance** (`procurement.suppliers`) | `GET /procurement/suppliers` | procurement.rfq.read | — | Dean Sheriff (2026-11-26) |
| **One supplier's performance and ratings** (`procurement.supplier`) | `GET /procurement/suppliers/{supplier_id}` | procurement.rfq.read | — | Dean Sheriff (2026-11-26) |
| **Procurement settings (approval tiers and quotes)** (`procurement.settings`) | `GET /procurement/settings` | procurement.settings.manage | — | Dean Sheriff (2026-11-26) |
| **Projects** (`projects.list`) | `GET /projects` | projects.time.log | — | Dean Sheriff (2026-11-26) |
| **One project (figures, budget, plan, team, ledger lines)** (`projects.project`) | `GET /projects/{project_id}` | projects.time.log | — | Dean Sheriff (2026-11-26) |
| **Set up or change a project** (`projects.project_form`) | `GET /projects/new` | projects.project.create | — | Dean Sheriff (2026-11-26) |
| **One project task** (`projects.task`) | `GET /projects/tasks/{task_id}` | projects.project.create | — | Dean Sheriff (2026-11-26) |
| **My weekly timesheet** (`projects.timesheet`) | `GET /projects/timesheet` | projects.time.log | — | Dean Sheriff (2026-11-26) |
| **Time approvals** (`projects.approvals`) | `GET /projects/approvals` | projects.time.approve | — | Dean Sheriff (2026-11-26) |
| **Project profitability** (`projects.profitability`) | `GET /projects/profitability` | projects.project.read | — | Dean Sheriff (2026-11-26) |
| **Project settings and hourly cost rates** (`projects.settings`) | `GET /projects/settings` | projects.settings.manage | — | Dean Sheriff (2026-11-26) |
| **Support tickets** (`support.tickets`) | `GET /support` | support.ticket.read | — | Dean Sheriff (2026-11-27) |
| **One support ticket (conversation, assignment, SLA)** (`support.ticket`) | `GET /support/{ticket_id}` | support.ticket.read | — | Dean Sheriff (2026-11-27) |
| **Raise a support ticket** (`support.ticket_new`) | `GET /support/new` | support.ticket.create | — | Dean Sheriff (2026-11-27) |
| **SLA report** (`support.sla_report`) | `GET /support/report` | support.report.read | — | Dean Sheriff (2026-11-27) |
| **Support settings (teams, categories, SLA targets, hours)** (`support.settings`) | `GET /support/settings` | support.settings.manage | — | Dean Sheriff (2026-11-27) |
| **My fleet (log fuel, book a pool vehicle, report an incident)** (`fleet.my`) | `GET /fleet` | fleet.vehicle.use | — | Dean Sheriff (2026-11-27) |
| **Vehicles** (`fleet.vehicles`) | `GET /fleet/vehicles` | fleet.vehicle.read | — | Dean Sheriff (2026-11-27) |
| **One vehicle (papers, service plans, fuel, incidents, costs)** (`fleet.vehicle`) | `GET /fleet/vehicles/{vehicle_id}` | fleet.vehicle.read | — | Dean Sheriff (2026-11-27) |
| **Add or edit a vehicle** (`fleet.vehicle_form`) | `GET /fleet/vehicles/new` | fleet.vehicle.manage | — | Dean Sheriff (2026-11-27) |
| **Vehicle bookings (approvals and what's booked)** (`fleet.bookings`) | `GET /fleet/bookings` | fleet.vehicle.manage | — | Dean Sheriff (2026-11-27) |
| **Vehicle incidents** (`fleet.incidents`) | `GET /fleet/incidents` | fleet.vehicle.use | — | Dean Sheriff (2026-11-27) |
| **One vehicle incident (photos, review and claim)** (`fleet.incident`) | `GET /fleet/incidents/{incident_id}` | fleet.vehicle.use | — | Dean Sheriff (2026-11-27) |
| **Drivers and their licences** (`fleet.drivers`) | `GET /fleet/drivers` | fleet.vehicle.read | — | Dean Sheriff (2026-11-27) |
| **Cost per vehicle** (`fleet.costs`) | `GET /fleet/costs` | fleet.cost.read | — | Dean Sheriff (2026-11-27) |
| **Fleet settings (alert lead times, fuel efficiency check)** (`fleet.settings`) | `GET /fleet/settings` | fleet.vehicle.manage | — | Dean Sheriff (2026-11-27) |
