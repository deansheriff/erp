# 0013. One policy engine: live grants, scopes, delegation and separation of duties

- Status: Accepted
- Date: 2026-11-18

## Context

Brief section 4. Every action and every row must be authorised by one engine, scoped to
the person's place in the organisation, with grants read live (never from a token).

## Decision

**Catalogue.** Each package declares its permissions in `policies.py` as `Permission`
constants (`module.resource[.subresource].action`). `app/authz/catalogue.py` collects them.
Migrations insert new keys into the `permissions` table. The app refuses to start if the
code and database catalogues differ, and a CI test checks the same thing.

**Roles.** Role templates live in code (`app/authz/role_templates.py`) and are read-only. Each
template is copied into the `roles` table (with `role_grants`), and the organisation can
edit its copies or create new roles. Two special cases:
- **Super Administrator** is protected: it grants everything, can't be edited or deleted,
  and at least one active person must always hold it.
- **Organisation Administrator** grants every permission except the `system` module
  (SMTP, branding, security settings). A migration that adds a permission also grants it
  to this role's copy, so it stays "everything except system settings".

**Assignments** have an optional department restriction, `valid_from` and `valid_to`
(acting and temporary roles), who assigned the role, and a required reason.

**Delegations** pass one permission, at a scope no wider than the delegator holds, to
another person for at most 90 days (`security_settings.delegation_max_days`). The scope
is evaluated from the **delegator's** position: a "team" delegation covers the delegator's
team. A delegation stops working as soon as the delegator no longer holds the
permission, and it ends when either person is suspended.

**Scopes.** `self`, `team`, `department` and `organization`. Team and department
membership comes from an `OrgResolver` protocol. Phase 3 ships a resolver that knows no
departments or teams, so those scopes match nothing beyond the person themselves.
Phase 4 plugs in the position-tree resolver; no caller changes.

**Enforcement.**
- `engine.check(db, user_id, permission, resource=None)` returns a `Decision` (allowed
  and a reason). An unknown permission key raises: it's a programming error.
- `engine.scope_filter(db, user_id, permission, Model)` returns a SQL clause for list and
  export queries. Models describe their owner and department columns with `__authz__`.
  Lists must filter in SQL, never in Python after loading.
- Routes declare `@requires(...)`. The access guard checks that the person holds the
  permission at some scope and audits every refusal; services then check the specific
  record.
- A record outside the person's scope answers **404**, exactly like a missing record
  (confirmed 2026-11-18).
- A grant restricted to a department never reaches a record without a department.
- Default deny.

**Nobody raises their own access.** You can only grant (to a role, a person or a
delegate) a permission you hold through your own roles, and no more widely than you hold
it. Delegated permissions can't be passed on again. Removing a role from someone follows
the same rule, so an Organisation Administrator can neither create nor remove a Super
Administrator. Delegations can't include `authz.*` or `system.*` permissions (a default,
to be confirmed by the product owner).

**Live grants.** Grants are loaded from the database for each request and cached for that
request. Across requests they're cached in Redis under
`authz:{user_id}:{authz_version}`. `authz_version` is a single counter that every change
to roles, grants, assignments or delegations increments, so a stale cache entry is never
read. Cached grants also expire at the next time-based change (an acting role starting
or ending, a delegation starting or ending). If Redis is unavailable, grants are loaded
from the database every time.

**Field classes.** These are `directory`, `personal`, `compensation`, `banking`,
`statutory` and `sensitive`. Each resource declares which field belongs to which class,
and `fields.readable_classes(...)` returns the classes the actor may read on a record.
`fields.redact(...)` drops hidden fields rather than blanking them, and treats an
unclassified field as `sensitive`, so forgetting to classify a field hides it.

**Separation of duties.** Rules are data (`SodRule`) declared beside each module's
permissions, and services check them with `sod.violations(...)`. Phase 3 adds one rule: nobody may
change their own role assignments.

**MFA by role.** A role can require two-step verification (templates: Super
Administrator, Organisation Administrator, Payroll Officer, Finance Officer, Finance
Manager). Anyone holding such a role must use it.

**Audit.** These are all audited:
- denials of sensitive permissions;
- every role, grant and assignment change;
- every delegation.

## Consequences

ADR 0010's interim flag is removed in the same change (ADR 0014). Phase 4 must supply the
real `OrgResolver`. Business modules add their permissions, template grants and SoD
rules in their own `policies.py`, plus a migration.
