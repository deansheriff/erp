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
