.PHONY: check lint test assets migrate screens-doc permissions-doc run

check: lint test

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports
	uv run python scripts/gen_screens_doc.py --check
	uv run python scripts/gen_permissions_doc.py --check

test:
	uv run pytest

assets:
	npm ci --no-audit --no-fund
	npm run build

migrate:
	uv run alembic upgrade head

screens-doc:
	uv run python scripts/gen_screens_doc.py

permissions-doc:
	uv run python scripts/gen_permissions_doc.py

run:
	uv run uvicorn app.main:app --reload
