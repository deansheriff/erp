# 0010. Interim super-admin flag until the policy engine (Phase 3)

- Status: Accepted (temporary; to be superseded in Phase 3)
- Date: 2026-11-17

## Context

Phase 2 needs a few administrator-only actions (invite staff, mail settings), but the
permission catalogue, roles and policy engine arrive in Phase 3.

## Decision

- `users.is_super_admin` marks the person created by the setup wizard. Further super
  admins are assigned through the Phase 3 role screens.
- `@requires("<permission>")` exists from Phase 2. Until Phase 3 its single rule is:
  *allow if the actor is an active, MFA-verified super admin*. Every non-public route
  already declares the exact permission it will need (for example
  `identity.user.invite`, `system.mail.manage`), so Phase 3 changes the engine, not the
  routes.
- Self-service routes (my sessions, my API keys, change my password) use
  `@requires_login` and act only on the signed-in user's own records.
- Phase 3 migrates every `is_super_admin = true` user into a non-deletable "Super
  Administrator" role assignment and drops the column, in the same change that
  introduces the engine. There will be one implementation at any time.

## Consequences

Phase 2 admin screens work and are tested now. The permission names chosen here become
part of the Phase 3 catalogue.
