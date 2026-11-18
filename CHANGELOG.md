# Changelog

All notable changes are recorded here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
  the Phase 3 policy engine (ADR 0010).
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
