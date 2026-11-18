# 0014. Super Administrator becomes a protected role

- Status: Accepted
- Date: 2026-11-18
- Supersedes: ADR 0010

## Decision

- The `users.is_super_admin` column is removed. Migration 0003 gives every user who had
  it an assignment of the protected **Super Administrator** role.
- The role grants every permission (now and in future) at organisation scope, requires
  two-step verification, and can't be edited, renamed or deleted.
- The last active holder can't be suspended, have the role ended, or end it themselves.
- Giving or removing a role needs every permission in it (ADR 0013), and only the Super
  Administrator role holds them all. So only a super admin can create or remove another
  super admin; the Organisation Administrator can't.
- The setup wizard assigns this role to the person it creates.

## Consequences

"Is this person a super admin?" is answered by the policy engine, like any other
question about access. There is one implementation.
