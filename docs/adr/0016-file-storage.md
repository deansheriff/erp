# 0016. One file service: purposes, scanning, authorised downloads, retention

- Status: Accepted
- Date: 2026-11-19

## Context

Brief section 5. Every document in the ERP (receipts, contracts, payslips, the
organisation logo) goes through one service in `app/files`. No other code may talk to
object storage or write to disk.

## Decision

**Storage.** Two private S3 buckets: `quarantine` (everything lands here) and `clean`
(only files that passed the virus scan). Object keys are random UUIDs, never derived from
the file name. `app/files/storage.py` is the only module that imports boto3.

**Purposes.** Each module registers its purposes in its own `files.py` (collected by
`app/files/purposes.py`, like the permission catalogue). A purpose declares:
allowed detected types, maximum size, the attach and view permissions on the parent
entity, the retention period, whether inline display is allowed, and the sensitivity
class. An upload for an unregistered purpose is refused.

**Upload.** The request body is parsed as it arrives (`app/files/web.py`) and refused
the moment it passes the purpose's size limit; the file is held in memory, never written
to disk (Starlette's form parser would spool it to a temporary file). Then: authorise
attach on the entity; check the quota; detect the type from the content (magic bytes;
SVG by parsing it as XML); refuse a mismatch with the purpose; hash it; store it in
quarantine; record it as `pending_scan`; queue the scan. Purposes keep their limits in
the tens of megabytes, so holding one upload in memory is safe; a purpose that needs
very large files would switch to streaming multipart uploads into S3.

Multipart requests must carry the CSRF token in the `X-CSRF-Token` header (HTMX sends
it on every request), so the CSRF check never parses an upload body.

**SVG.** Allowed for the organisation logo (confirmed 2026-11-19). An SVG is parsed with
defusedxml and refused if it contains scripts, event-handler attributes, `foreignObject`,
or links to anything but local fragments. SVGs are always served with
`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`.

**Scanning.** A Celery task streams the object to ClamAV (`clamd` INSTREAM over TCP).
Clean files are copied to the clean bucket and marked `clean`. Infected files stay in
quarantine, are marked `quarantined`, audited, and reported to the uploader and to
holders of `files.file.manage` (confirmed 2026-11-19). If the scanner can't be reached
the file stays `pending_scan` and the task retries: the service fails closed.
Development and tests may use an accept-all scanner; the app refuses to start with it in
production.

**Download.** `GET /files/{id}` checks view permission on the linked entity with
`authz.check` (any link that allows it is enough), then streams the clean object.
Responses carry the detected type, `X-Content-Type-Options: nosniff`, and
`Content-Disposition: attachment` unless the purpose allows inline display. Streaming
through the app (rather than presigned redirects) keeps the storage endpoint private and
every download authorised and auditable. Downloads of `compensation`, `banking`,
`statutory` and `sensitive` files are audited.

**Versions.** Replacing a document creates a new version in the same version group; old
versions stay readable to anyone who may view the entity.

**Deletion and retention.** Delete is a soft delete. A nightly job hard-deletes files
whose `retention_until` has passed, unless they are on legal hold.

**Quota.** One organisation quota, 50 GB by default (confirmed 2026-11-19), stored in
`storage_settings` and editable. Usage is tracked on upload and on hard deletion, in the
same transaction. An upload that would exceed it is refused.

**Local development.** No Docker or ClamAV on the development machine: tests use moto
(an S3 emulator) and the accept-all scanner. Real MinIO and ClamAV run in
docker-compose and CI.

## Consequences

Modules never handle bytes or buckets themselves: they register purposes and call
`files.service`. The scanner and storage are swappable behind small interfaces.
