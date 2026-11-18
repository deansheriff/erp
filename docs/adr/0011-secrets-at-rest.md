# 0011. Secrets at rest: hashing and encryption

- Status: Accepted
- Date: 2026-11-17

## Decision

| Secret | Treatment | Why |
|---|---|---|
| Passwords, MFA recovery codes | Argon2id (`argon2-cffi` defaults: time 3, memory 64 MiB, parallelism 4) | Slow hashing; never needs to be read back |
| Session tokens, invitation and reset tokens, API keys | SHA-256 of 256-bit random values | High-entropy values: a fast hash is enough and keeps lookups indexed |
| TOTP secrets, SMTP password | Fernet (AES-128-CBC + HMAC-SHA256, `cryptography`) with `KX_ENCRYPTION_KEY` | Must be read back to verify codes or log in to the mail server |

- `KX_ENCRYPTION_KEY` is a Fernet key from the environment (`uv run kx generate-key
  --fernet`). Several comma-separated keys may be given; the first encrypts, all decrypt
  (rotation).
- Decrypted values never leave the service that needs them; they are never rendered back
  to the browser. Forms show "Saved — enter a new value to replace".

## Consequences

Losing `KX_ENCRYPTION_KEY` means users re-enrol MFA and the SMTP password is re-entered;
no business data is lost.
