# Changelog

All notable changes are recorded here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
