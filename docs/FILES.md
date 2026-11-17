# Files

> Built in Phase 5. This document will describe the storage service, file purposes,
> scanning, download authorisation, retention, legal hold and quotas.

Already in force from Phase 1:
- MinIO runs in docker-compose with two private buckets, `kx-quarantine` and `kx-clean`.
- No code outside `app/files` may import an S3 client or write to disk. This is enforced
  by `tests/ci/test_code_rules.py` and import-linter.
- Uploads will show their scan state with the shared uploader component
  (`ui.file_row` in `app/web/templates/components/ui.html`).
