# Kxmeleon ERP

HR, payroll, leave, attendance, expenses, finance, inventory, procurement, projects,
support, fleet and collaboration for Nigerian businesses. **One installation serves one
organisation** ([ADR 0002](docs/adr/0002-single-organisation-deployment.md)).

## Run it locally (about 10 minutes)

You need **Docker Desktop** (with Compose v2) and **git**. Nothing else is required to
run the app.

```bash
git clone <repo-url> kxmeleon-erp && cd kxmeleon-erp
cp .env.example .env
docker compose up --build
```

Then open:

| What | Where |
|---|---|
| The app | http://localhost:8000 |
| Mail catcher (Mailpit) | http://localhost:8025 |
| Object storage console (MinIO) | http://localhost:9001 |

The `migrate` service creates the database schema before the app starts. The app then
prints a one-time **setup code** in its log (`docker compose logs app | grep setup_code`).
Open http://localhost:8000, enter the code, and the wizard creates the organisation, the
super admin and the mail settings. For local mail use host `mailpit`, port `1025`,
security "None", and read the messages at http://localhost:8025. See
[docs/INSTALLATION.md](docs/INSTALLATION.md).

## Develop

You also need [uv](https://docs.astral.sh/uv/) and Node.js 20 or later.

```bash
uv sync                       # creates .venv with Python 3.12 and every dependency
npm ci && npm run build       # builds CSS, vendored JS and fonts into app/web/static
docker compose up -d db redis minio minio-init clamav mailpit
uv run alembic upgrade head   # as kx_migrator (KX_MIGRATOR_DATABASE_URL)
uv run uvicorn app.main:app --reload
```

### Checks (the same ones CI runs)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run pytest                 # needs Postgres: KX_TEST_DATABASE_URL (see .env.example)
uv run python scripts/gen_screens_doc.py --check
```

`make check` runs all of them on Linux and macOS.

## Layout

```
app/core      config, database, logging, errors, middleware, IDs
app/audit     append-only audit log
app/authz     permission engine (Phase 3); route declarations (@public) today
app/files     the only file storage service (Phase 5)
app/modules   business modules (HR, payroll, …), each with the same internal layout
app/web       layout shell, shared components, error pages, static assets
migrations    Alembic
design/stitch DESIGN.md, screens.yaml, Stitch HTML exports
docs          ADRs, installation, permissions, files, screens
tests         ci/ (repository rules), db/, web/
```

## Rules that CI enforces

- Every route declares a permission or is explicitly `@public`.
- No `session.query()`, no `async def` routes, no `str(exception)` in responses.
- Modules talk to each other only through their `service.py`.
- Only `app/files` may use object storage or write to disk.
- Every HTML route has an entry in `design/stitch/screens.yaml`, and its Stitch export
  is committed.
