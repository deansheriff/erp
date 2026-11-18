# Installation model

Kxmeleon ERP is installed **once per organisation** (ADR 0002). One installation means
one PostgreSQL database, one set of object-storage buckets, one Redis and one set of
secrets, serving exactly one company.

## What an installation consists of

| Service | Purpose | Development (docker-compose) |
|---|---|---|
| `app` | FastAPI web app (Uvicorn) | `localhost:8000` |
| `worker` | Celery worker (mail, scans, reports, scheduled jobs) | — |
| `beat` | Celery beat scheduler (added when the first scheduled job lands) | — |
| `migrate` | One-shot `alembic upgrade head` as `kx_migrator`, runs before `app` | — |
| `db` | PostgreSQL 16 | `localhost:5432` |
| `redis` | Celery broker, cache, WebSocket fan-out | `localhost:6379` |
| `minio` | S3-compatible object storage | API `localhost:9000`, console `localhost:9001` |
| `clamav` | Virus scanning for uploads | `localhost:3310` |
| `mailpit` | Development-only SMTP catcher | SMTP `localhost:1025`, UI `localhost:8025` |

In production, any S3-compatible store and any SMTP provider can be used. Mailpit is
never deployed.

## Secrets and configuration

All configuration comes from environment variables prefixed `KX_` (see `.env.example`).
Required secrets:

- `KX_SECRET_KEY`: signs session and CSRF tokens.
- `KX_ENCRYPTION_KEY`: encrypts secrets stored in the database, such as the SMTP password.
- `KX_DATABASE_URL`: `kx_app` credentials.
- `KX_MIGRATOR_DATABASE_URL`: `kx_migrator` credentials.
- The S3 credentials.

Generate keys with `uv run kx generate-key`. Losing `KX_ENCRYPTION_KEY` means the SMTP
password must be re-entered. No business data is encrypted with it.

## First run

1. `docker compose up`. The `migrate` service creates the schema, and the app starts in
   **uninstalled** mode.
2. The app log prints a one-time **setup code** (`setup_code` in the JSON log line). To
   get a fresh one, run `docker compose exec app kx setup-token`. Each code is valid for
   60 minutes; issuing a new one, or restarting the app, invalidates the old one. If that
   happens part-way through, the wizard asks for the new code and carries on.
3. Open `http://localhost:8000/setup`. Every other page redirects there until setup is
   complete.
4. **Step 1: Organisation.** Legal name, trading name, RC number, TIN, address,
   timezone (default Africa/Lagos), base currency (NGN) and fiscal year start.
5. **Step 2: Super admin.** Name and work email. You choose your own password
   (Argon2id) and enrol TOTP MFA. There is never a default password.
6. **Step 3: Mail (SMTP).** Host, port, security (STARTTLS, SSL/TLS or none for local
   relays only), username, password, from name and address, and reply-to. "Send test
   email" must succeed before it is saved. This step can be skipped and done later
   under *Settings → Mail*, but invitations and password resets cannot be sent until it
   is done.
7. Finishing the wizard consumes the token, writes the first audit entries, and locks
   `/setup` permanently.

## Super admin

- The super admin is a protected system role. It holds every permission, including
  `system.*` settings (mail, storage, security policy, AI opt-in).
- There must always be at least one active super admin. The last one cannot be
  suspended, demoted or deleted.
- MFA is mandatory. Every action is audited like any other user's.
- Super admins invite other staff, who receive a single-use invitation email and set
  their own passwords.

## Upgrades

Pull the new image and run `docker compose up -d`. `migrate` runs before `app`, and
migrations must be safe to run unattended. Back up the database and buckets first.
