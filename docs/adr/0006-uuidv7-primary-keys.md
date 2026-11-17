# 0006. UUIDv7 primary keys

- Status: Accepted
- Date: 2026-11-17

## Context

Record IDs appear in URLs. Sequential integers leak volumes (how many employees or
claims exist) and are guessable. Random UUIDv4 keys fragment B-tree indexes.

## Decision

Every table's primary key is `id UUID`, generated in the application as **UUIDv7**
(time-ordered, RFC 9562) by `app.core.ids.new_id()`. This is a small in-house function,
because Python 3.12's `uuid` module has no v7. Human-facing document numbers
(`EXP-2026-000142`) are separate, gap-free columns (brief section 8).

## Consequences

IDs are unguessable enough for URLs (authorisation is still always checked), sort by
creation time, and index well.
