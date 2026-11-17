# 0001. Record architecture decisions

- Status: Accepted
- Date: 2026-11-17

## Context

Kxmeleon ERP is built in phases over a long period. Decisions made early (tenancy model,
request handling, tooling) shape everything after them, and need to be findable later
with their reasons.

## Decision

Every significant decision is recorded as a short ADR in `docs/adr/NNNN-title.md`, using
this layout: Status, Date, Context, Decision, Consequences. ADRs are never edited after
acceptance except to change their status. A changed decision gets a new ADR that
supersedes the old one.

## Consequences

Reviewers can trace why the code is shaped the way it is. Reversing a decision is an
explicit, reviewable act.
