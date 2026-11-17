# 0003. uv for Python packaging and environments

- Status: Accepted
- Date: 2026-11-17

## Context

The brief allows Poetry or uv. We need one locked, reproducible toolchain that works on
Windows developer machines, in Docker and in CI, and that installs only from public PyPI.

## Decision

Use **uv** (`pyproject.toml` + committed `uv.lock`). Python is pinned to 3.12 via
`.python-version`; uv downloads it when the machine has a different interpreter. All
commands run through `uv run ...`. Dependency changes go through `uv add` / `uv remove`,
never by editing the lock file.

## Consequences

Fast installs and one tool for interpreter, venv and lock. CI uses
`astral-sh/setup-uv` and `uv sync --frozen`, so a stale lock fails the build.
