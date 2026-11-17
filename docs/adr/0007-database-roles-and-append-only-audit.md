# 0007. Separate database roles and an append-only audit log

- Status: Accepted
- Date: 2026-11-17

## Context

Without multi-tenancy (ADR 0002), we still want the application's own database
credentials to be unable to rewrite history or change the schema.

## Decision

| Role | Login | Owns objects | Used by |
|---|---|---|---|
| `kx_owner` | no | yes | Owns every table. Nobody connects as it. |
| `kx_migrator` | yes | member of `kx_owner` | Alembic only (`migrate` container, CI). |
| `kx_app` | yes | no | Web app and Celery workers. |

- `kx_app` is not a superuser, cannot create roles or databases, and owns nothing. It
  gets `SELECT, INSERT, UPDATE, DELETE` on business tables through default privileges set
  by the migrator. It never gets `TRUNCATE` or DDL.
- On `audit_log`, `kx_app` gets **`INSERT, SELECT` only**. `SELECT` is needed by the
  audit viewer, which itself requires `audit.log.read`. A trigger also rejects `UPDATE`,
  `DELETE` and `TRUNCATE` for every role, including the owner, so history cannot be
  rewritten without a deliberate migration that drops the trigger.
- At start-up the app checks it is connected as a role that is not a superuser and owns
  no tables, and refuses to start otherwise.
- Denied-permission audit events are written in their **own** short transaction, so they
  survive the rollback of the request that was denied.

## Consequences

A compromised application credential can damage current data but cannot erase the audit
trail or alter the schema. Developers run migrations with a different connection string
(`KX_MIGRATOR_DATABASE_URL`) from the app (`KX_DATABASE_URL`).
