# Identity, sessions and installation

Phase 2. Covers users, invitations, sign-in, MFA, sessions, CSRF, password reset, API keys,
the first-run setup wizard and mail (SMTP) settings. Brief section 7; ADRs 0002, 0009–0012.

## 1. Security settings (confirmed 2026-11-17)

Stored in the singleton `security_settings` row, editable later by a super admin. These are
the values the product owner confirmed:

| Setting | Value |
|---|---|
| Session idle timeout | 30 minutes |
| Session absolute lifetime | 12 hours |
| Lockout | 5 consecutive failures → locked 15 minutes, auto-unlocks; an admin can unlock sooner |
| Password rule | at least 12 characters; common passwords and passwords built from the person's own name, email or the organisation name are rejected; no composition rules, no expiry |
| Invitation link | 72 hours, single use |
| Password reset link | 30 minutes, single use |
| Sign-in rate limit per IP **(confirm)** | 20 attempts per 15 minutes |

## 2. Data model

| Table | Purpose |
|---|---|
| `organization` | Singleton (one row, enforced by a check constraint): legal and trading name, RC number, TIN, address, timezone, base currency, fiscal year start month. |
| `installation` | Singleton: `installed_at`, plus the hash and expiry of the current setup token. |
| `users` | Email (unique, case-insensitive), full name, `status` (`invited`, `active`, `suspended`, `deactivated`), Argon2id `password_hash`, encrypted TOTP secret, `mfa_enabled_at`, failure counter, `locked_until`. Who is a super admin is a role assignment (ADR 0014). |
| `user_sessions` | Server-side sessions: SHA-256 of the cookie token, CSRF token, created/last-seen/absolute-expiry, `mfa_satisfied`, IP, user agent, `revoked_at` and reason. |
| `user_tokens` | Single-use emailed tokens (`invite`, `password_reset`): SHA-256 hash, expiry, `used_at`. |
| `mfa_recovery_codes` | Ten one-time codes per user, Argon2id-hashed. |
| `login_attempts` | Per-IP attempt log for rate limiting (pruned after 24 hours). |
| `api_keys` | Hash of the key, prefix for display, owner, name, permissions (list, enforced from Phase 3), expiry, last used, revoked. |
| `mail_settings` | Singleton SMTP configuration; password encrypted (ADR 0011). |
| `mail_outbox` | Queued emails with status, attempts, last error code (never the message body of a failure). |
| `security_settings` | Singleton of the values in section 1. |

## 3. Flows

**Installation (ADR 0002).** While `installation.installed_at` is empty, every route except
`/setup`, `/healthz`, `/readyz` and `/static` redirects to `/setup`. The setup token is
printed to the log at start-up and by `kx setup-token`. The wizard has three steps:
1. organisation;
2. super admin account, including password and TOTP enrolment;
3. SMTP, which can be skipped.

Finishing signs the super admin in, consumes the token, audits the event and closes
`/setup` for good.

**Sign-in.**
1. Rate-limit check by IP.
2. Look up the user by email. An unknown email still goes through a dummy Argon2 check so
   timing reveals nothing.
3. Locked or not active: the same generic message for every case.
4. Wrong password: increment the failure counter, lock at the threshold, audit.
5. Correct password: reset the counter and create a session with `mfa_satisfied = false`.
   Then send the user to verify their TOTP if MFA is enrolled, or to enrol if MFA is
   required but not set up.
6. Only an MFA-satisfied session reaches the app.

Two-step verification is required for anyone holding a role that requires it (Super
Administrator, Organisation Administrator, Payroll Officer, Finance Officer and Finance
Manager by default; editable per role, but only a holder of `system.security.manage` may
switch the requirement off).

**Sessions.**
- The cookie `kx_session` is random (32 bytes), `HttpOnly`, `Secure` (except over local
  HTTP), `SameSite=Lax`, host-only.
- Every request checks the idle and absolute expiry and refreshes `last_seen_at` at most
  once a minute.
- Users see and revoke their own sessions. Signing out revokes the current one.
- Changing your password revokes all your other sessions.
- Suspending a user revokes all their sessions and API keys at once.

**CSRF (ADR 0009).** A synchronizer token is kept in the session. It is checked on every
`POST`, `PUT`, `PATCH` and `DELETE`, either from the `X-CSRF-Token` header (HTMX sends it
via `hx-headers` on `<body>`) or from the `csrf_token` form field. The `Origin` header must
match `KX_BASE_URL`. Requests authenticated by an API key are exempt, since they carry no
cookie.

**Invitations.**
1. An admin enters name and email.
2. The app creates the user as `invited` with a 72-hour token and emails the link.
3. Accepting sets the person's own password, then MFA if required, then activates the
   account.
4. "Resend" invalidates the old token. Admins never see or set passwords.

**Password reset.**
- The response is always "If that address has an account, we've sent a link." Nothing
  about whether the account exists is revealed.
- The link is valid for 30 minutes and works once.
- Completing it revokes all sessions and audits the event.

**API keys.**
- Format: `kx_<8-char prefix>_<secret>`. Shown once, stored as a SHA-256 hash, can carry an
  expiry, revocable.
- Sent as `Authorization: Bearer …` to `/api/*` only.
- They carry an explicit permission list. From Phase 3, `authz.check` enforces it as a
  subset of the owner's own permissions.

**Mail (ADR 0012).** Application email (invitations, resets) is written to `mail_outbox` in
the same transaction and sent by a worker. "Send test email" in settings sends immediately,
with a 10-second timeout, and shows a plain-language result.

## 4. Audit

Recorded events:
- `identity.login` (success, failure, locked, rate-limited)
- `identity.logout`
- `identity.mfa.enrol`
- `identity.mfa.verify` (failure)
- `identity.recovery_code.use`
- `identity.password.change`
- `identity.password.reset`
- `identity.session.revoke`
- `identity.user.invite`, `identity.user.invite_resend`, `identity.user.activate`
- `identity.user.suspend`
- `identity.api_key.create`, `identity.api_key.revoke`
- `setup.complete`
- `settings.mail.update`, `settings.mail.test`

Secrets (passwords, TOTP secrets, tokens, SMTP password) never appear in audit rows or logs.
