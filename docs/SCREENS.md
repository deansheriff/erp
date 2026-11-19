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
