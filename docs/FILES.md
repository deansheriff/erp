# Files

Phase 5. Decisions are in ADR 0016.

## Tables

| Table | What it holds |
|---|---|
| `files` | Random storage key, sanitised original name, size, SHA-256, declared and detected type, status (`pending_scan`, `clean`, `infected`, `quarantined`, `deleted`), classification, purpose, uploader, timestamps, `retention_until`, `legal_hold`, version group and version, scan result. |
| `file_links` | File, entity type and ID, purpose, who linked it and when. |
| `storage_settings` | The organisation quota (default 50 GB) and the bytes in use. |

## Purposes

| Purpose | Types | Max size | Attach | View | Retention | Inline | Class |
|---|---|---|---|---|---|---|---|
| `organization_logo` | PNG, JPEG, WebP, SVG (sanitised) | 2 MB | `system.branding.manage` | any signed-in person | 1 year after it's replaced or deleted (confirm) | yes | directory |

Later modules add their own purposes (expense receipts, employee documents, payslips,
invoices) in their `files.py`.

## Flows

**Upload:** authorise attach → check quota → stream into quarantine with the size limit
→ hash and detect type → refuse a mismatch → `pending_scan` → scan.

**Scan:** clean → copied to the clean bucket, `clean`. Infected → `quarantined`, audited,
uploader and file managers alerted. Scanner unreachable → stays `pending_scan`, retried.

**Download:** `GET /files/{id}` → view permission on a linked entity → stream with the
detected type, `nosniff`, and `attachment` unless inline is allowed. Sensitive classes
are audited.

**Delete:** soft delete; the nightly retention job hard-deletes after `retention_until`
unless on legal hold. Replacing creates a new version; old versions stay readable.

## Rules enforced in CI

- Nothing outside `app/files` imports boto3/botocore or writes to disk
  (`tests/ci/test_code_rules.py`, import-linter).
