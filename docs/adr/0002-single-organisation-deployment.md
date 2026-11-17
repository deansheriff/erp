# 0002. One deployment per organisation (no multi-tenancy)

- Status: Accepted
- Date: 2026-11-17
- Supersedes: the multi-tenant design in the original build brief (sections 3 and
  the platform realm)

## Context

The original brief described one deployment serving many client organisations, with
PostgreSQL Row-Level Security, an ORM tenant guard and a separate platform-operator realm.
The product owner decided on 2026-11-17 that Kxmeleon ERP is instead **installed once per
organisation**. Each installation serves exactly one company, set up by a **super admin**
account, with outgoing mail through an **SMTP server configured by that super admin**.

## Decision

1. **One organisation per installation.** There is no `organization_id` column on business
   tables, no Row-Level Security, no tenant context and no platform realm. Isolation
   between client companies is achieved by separate installations (separate database,
   object storage buckets, Redis and secrets).
2. **The organisation is a singleton row** (`organization`, constrained to one row). It
   holds legal name, RC number, TIN, timezone, base currency and document branding.
3. **Installation is a first-run wizard** (built in Phase 2 with identity):
   - When the database has no organisation, every route except `/setup`, `/healthz`
     and static assets redirects to `/setup`.
   - `/setup` requires a **setup token**. `uv run kx setup-token` (or the app log on
     first start) prints a single-use token valid for 60 minutes. This stops anyone who
     reaches the server first from claiming the installation.
   - The wizard collects organisation details, then creates the **super admin** (the
     person sets their own password and enrols TOTP MFA; there is no default password),
     then optionally configures SMTP and sends a test email.
   - Completing the wizard consumes the token and locks `/setup` permanently.
4. **Super admin** is a protected, non-deletable system role ("Super Administrator").
   It holds every permission, including the `system.*` permissions (SMTP, storage,
   security policy, AI opt-in) that no other template role has. At least one active
   super admin must always exist; the last one cannot be suspended or demoted. MFA is
   mandatory. All the authorisation rules (scopes, field classes, separation of duties,
   audit) still apply to it: it can do everything, but every action is audited.
5. **Mail is SMTP only**, configured in the UI by a super admin (host, port, TLS mode,
   username, password, from address and name, reply-to). The SMTP password is encrypted
   at rest with a key from the environment (`KX_ENCRYPTION_KEY`), never shown again after
   saving, and changes are audited. Mail is sent from a Celery worker through an outbox
   table with retries. In development, docker-compose includes Mailpit as a local SMTP
   catcher. Mail settings ship in Phase 2, because invitations and password resets need
   them.
6. **What stays from the brief:** the central policy engine, scopes (`self`, `team`,
   `department`, `organization`, where `organization` now means the whole installation),
   field-level sensitivity, the files service, append-only audit, separation of duties,
   Stitch-first screens, and real-Postgres tests.
7. **Database roles stay separated** (see ADR 0007) even without tenancy: the application
   never connects as the table owner, and the audit log stays append-only.

## Consequences

- Much less machinery: no RLS policies, tenant guard, generated cross-tenant route tests
  or platform sessions. The Phase 1 scope shrinks accordingly.
- Data of different client companies can never mix, because they never share a database.
- Each client needs its own deployment. Upgrades are rolled out per installation, so
  migrations must be safe to run unattended (`migrate` container on start-up).
- Should multi-tenancy be wanted later, it is a large change (an `organization_id` on
  every table plus RLS). This ADR would be superseded, not amended.
- The phase plan changes:
  - Phase 1 is foundation only: tooling, CI, docker-compose, config, database, audit,
    errors, Stitch design system and app shell.
  - Phase 2 is identity, plus the installation wizard, super admin and SMTP settings.
