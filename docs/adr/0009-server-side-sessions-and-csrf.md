# 0009. Server-side sessions with synchronizer-token CSRF

- Status: Accepted
- Date: 2026-11-17

## Context

The brief requires server-side sessions with idle and absolute timeouts, user-visible
revocation, and CSRF protection on every state-changing request. Tokens must carry
identity only, never permissions.

## Decision

- A session is a row in `user_sessions`. The cookie holds 32 random bytes (URL-safe);
  the database holds only their SHA-256, so a database leak does not yield usable cookies.
- Cookie: `kx_session`, `HttpOnly`, `SameSite=Lax`, host-only, `Path=/`. `Secure` is set
  whenever `KX_BASE_URL` is https (so local `http://localhost` development still works).
- Each session row stores a random CSRF token. Unsafe methods must present it in the
  `X-CSRF-Token` header or the `csrf_token` form field, compared in constant time, and the
  `Origin` (or `Referer`) must match `KX_BASE_URL`. This is checked in one middleware, not
  per route.
- A new session is created after password verification (session fixation is impossible:
  pre-login requests have no session). MFA completion upgrades that same session by
  setting `mfa_satisfied`.
- No JWTs. Permissions are read from the database per request (Phase 3).

## Consequences

One database read per request to load the session (an indexed lookup by hash). Revocation
is immediate. Horizontal scaling needs no shared secret beyond the database.
