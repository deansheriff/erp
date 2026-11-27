"""The file service (ADR 0016). The one way to store, scan, serve, delete and retain a
file. Modules register purposes (app/files/purposes.py) and call these functions; they
never handle buckets or bytes themselves."""

import hashlib
import logging
import re
import unicodedata
import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.audit.service import AuditEvent, record, record_by
from app.authz import engine
from app.authz.fields import FieldClass
from app.core import clock
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.core.formatting import format_size
from app.files import detect, storage
from app.files.models import FileLink, StorageSettings, StoredFile
from app.files.purposes import FilePurpose
from app.files.purposes import get as get_purpose
from app.files.scanner import ScannerUnavailable, get_scanner
from app.files.schemas import Download, FilePage, FileView, LinkView, Usage
from app.identity.models import User
from app.identity.schemas import Actor
from app.mail import service as mail
from app.org import service as org

log = logging.getLogger("app.files")

PAGE_SIZE = 50
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*]+")


# ------------------------------------------------------------------ helpers


def sanitise_filename(name: str | None) -> str:
    """A display-only name: no path, no control or reserved characters, at most 255."""
    name = unicodedata.normalize("NFC", name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME.sub("_", name).strip(" .")
    if not name:
        return "file"
    if len(name) > 255:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 250 - len(ext)] + dot + ext) if dot and len(ext) <= 10 else name[:255]
    return name


def _purpose(key: str) -> FilePurpose:
    purpose = get_purpose(key)
    if purpose is None:
        raise ValidationFailed("Files can't be attached here.", code="unknown_purpose")
    return purpose


def _settings_row(db: Session, *, lock: bool = False) -> StorageSettings:
    stmt = select(StorageSettings)
    return db.scalars(stmt.with_for_update() if lock else stmt).one()


def _authorise(
    db: Session, actor: Actor, permission: str | None, purpose: FilePurpose, entity_id: str
) -> bool:
    resource = purpose.resolve(db, entity_id)
    if resource is None:
        return False
    if purpose.allow is not None and not purpose.allow(db, actor.user_id, entity_id):
        return False
    return permission is None or bool(engine.check(db, actor.user_id, permission, resource))


def _view(db: Session, row: StoredFile, version_count: int | None = None) -> FileView:
    purpose = get_purpose(row.purpose)
    links = [
        LinkView(link.entity_type, link.entity_id, link.purpose)
        for link in db.scalars(select(FileLink).where(FileLink.file_id == row.id))
    ]
    uploader = db.get(User, row.uploaded_by) if row.uploaded_by else None
    if version_count is None:
        version_count = (
            db.scalar(
                select(func.count())
                .select_from(StoredFile)
                .where(StoredFile.version_group_id == row.version_group_id)
            )
            or 1
        )
    return FileView(
        id=row.id,
        original_filename=row.original_filename,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        declared_mime=row.declared_mime,
        detected_mime=row.detected_mime,
        status=row.status,
        classification=row.classification,
        purpose=row.purpose,
        purpose_label=purpose.label if purpose else row.purpose,
        uploaded_by=uploader.full_name if uploader else None,
        created_at=row.created_at,
        scanned_at=row.scanned_at,
        scan_result=row.scan_result,
        deleted_at=row.deleted_at,
        retention_until=row.retention_until,
        legal_hold=row.legal_hold,
        version=row.version,
        version_count=version_count,
        links=links,
    )


# ------------------------------------------------------------------ upload


def upload(
    db: Session,
    actor: Actor,
    *,
    purpose_key: str,
    entity_id: str,
    filename: str | None,
    declared_mime: str | None,
    data: bytes,
    replaces: uuid.UUID | None = None,
) -> uuid.UUID:
    """Store a new file (or a new version of `replaces`) in quarantine, pending a scan."""
    purpose = _purpose(purpose_key)
    if purpose.resolve(db, entity_id) is None:
        raise NotFound()
    if not _authorise(db, actor, purpose.attach_permission, purpose, entity_id):
        raise PermissionDenied()
    if not data:
        raise ValidationFailed(field_errors={"file": "Choose a file to upload."})
    if len(data) > purpose.max_bytes:
        raise ValidationFailed(
            field_errors={
                "file": f"This file is too large. The limit is {format_size(purpose.max_bytes)}."
            }
        )
    detected = detect.detect(data)
    if detected is None or detected not in purpose.allowed_types:
        raise ValidationFailed(field_errors={"file": "This type of file isn't accepted here."})
    if detected == detect.SVG:
        try:
            detect.check_svg(data)
        except detect.UnsafeSvg as exc:
            raise ValidationFailed(field_errors={"file": exc.message}) from exc

    settings = _settings_row(db, lock=True)
    if settings.quota_bytes is not None and settings.used_bytes + len(data) > settings.quota_bytes:
        raise Conflict("The organisation's storage is full. Ask an administrator to free space.")

    group_id, version = uuid.uuid4(), 1
    if replaces is not None:
        previous = db.get(StoredFile, replaces)
        linked = previous is not None and db.scalar(
            select(func.count())
            .select_from(FileLink)
            .where(
                FileLink.file_id == replaces,
                FileLink.entity_id == entity_id,
                FileLink.purpose == purpose.key,
            )
        )
        if previous is None or not linked:
            raise NotFound()
        group_id = previous.version_group_id
        version = (
            db.scalar(
                select(func.max(StoredFile.version)).where(StoredFile.version_group_id == group_id)
            )
            or 0
        ) + 1
        # Older versions are retired only once this one passes the scan (_retire_older).

    key = uuid.uuid4().hex
    storage.put(storage.quarantine_bucket(), key, data, detected)
    row = StoredFile(
        storage_key=key,
        original_filename=sanitise_filename(filename),
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        declared_mime=(declared_mime or None),
        detected_mime=detected,
        status="pending_scan",
        classification=purpose.classification,
        purpose=purpose.key,
        uploaded_by=actor.user_id,
        version_group_id=group_id,
        version=version,
        retention_until=clock.now() + purpose.expires_after if purpose.expires_after else None,
    )
    db.add(row)
    db.flush()
    db.add(
        FileLink(
            file_id=row.id,
            entity_type=purpose.entity_type,
            entity_id=entity_id,
            purpose=purpose.key,
            linked_by=actor.user_id,
        )
    )
    settings.used_bytes += len(data)
    record_by(
        db,
        actor,
        "files.file.upload",
        entity_type="file",
        entity_id=str(row.id),
        changes={
            "purpose": {"from": None, "to": purpose.key},
            "name": {"from": None, "to": row.original_filename},
            "size_bytes": {"from": None, "to": row.size_bytes},
        },
    )
    return row.id


def request_scan() -> None:
    """Ask a worker to scan soon. Best effort: the beat schedule sweeps pending files
    anyway, so a missing broker only delays scanning."""
    try:
        from app.files import tasks  # noqa: PLC0415  (avoid importing Celery in every request)

        tasks.scan_pending.apply_async(retry=False)
    except Exception:  # noqa: BLE001
        log.info("scan_request_deferred")


# ------------------------------------------------------------------ scanning


def scan_pending(limit: int = 20) -> int:
    """Scan files waiting in quarantine. Safe to run concurrently (SKIP LOCKED). Returns
    how many were finished (clean or quarantined)."""
    done = 0
    scanner = get_scanner()
    for _ in range(limit):
        with session_scope() as db:
            row = db.scalars(
                select(StoredFile)
                .where(StoredFile.status == "pending_scan")
                .order_by(StoredFile.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if row is None:
                break
            try:
                result = scanner.scan(storage.stream(storage.quarantine_bucket(), row.storage_key))
            except ScannerUnavailable:
                log.warning("scanner_unavailable", extra={"file_id": str(row.id)})
                break  # stays pending; retried on the next sweep
            row.scanned_at = clock.now()
            if result.clean:
                storage.copy(storage.quarantine_bucket(), storage.clean_bucket(), row.storage_key)
                row.status, row.scan_result = "clean", None
                _retire_older(db, row)
                record(
                    db,
                    AuditEvent(
                        action="files.file.scan",
                        actor_type="system",
                        entity_type="file",
                        entity_id=str(row.id),
                    ),
                )
            else:
                row.status, row.scan_result = "quarantined", result.detail
                record(
                    db,
                    AuditEvent(
                        action="files.file.scan",
                        actor_type="system",
                        outcome="failure",
                        entity_type="file",
                        entity_id=str(row.id),
                        reason=result.detail,
                    ),
                )
                _alert_quarantined(db, row)
            done += 1
    return done


def _retire_older(db: Session, row: StoredFile) -> None:
    """A new version passed its scan: older versions stay readable until their
    retention period ends, then the retention job removes them."""
    purpose = get_purpose(row.purpose)
    if purpose is None:
        return
    for older in db.scalars(
        select(StoredFile).where(
            StoredFile.version_group_id == row.version_group_id,
            StoredFile.version < row.version,
            StoredFile.retention_until.is_(None),
        )
    ):
        older.retention_until = clock.now() + purpose.retention


def _alert_quarantined(db: Session, row: StoredFile) -> None:
    """Tell the uploader and everyone who manages files (confirmed 2026-11-19)."""
    uploader = db.get(User, row.uploaded_by) if row.uploaded_by else None
    managers = [
        u
        for u in db.scalars(select(User).where(User.status == "active"))
        if engine.check(db, u.id, "files.file.manage")
    ]
    recipients: dict[uuid.UUID, tuple[User, bool]] = {m.id: (m, True) for m in managers}
    if uploader is not None and uploader.status == "active":
        recipients.setdefault(uploader.id, (uploader, False))
    if not mail.is_configured(db):
        log.warning("quarantine_alert_not_sent_mail_unconfigured", extra={"file_id": str(row.id)})
        return
    link = get_settings().base_url.rstrip("/") + f"/admin/files?selected={row.id}"
    organisation = org.get_organization(db)
    for user, is_manager in recipients.values():
        mail.queue(
            db,
            to=user.email,
            template="file_quarantined",
            context={
                "name": user.full_name.split()[0],
                "filename": row.original_filename,
                "uploader": uploader.full_name if uploader else "Unknown",
                "signature": row.scan_result or "a threat",
                "is_manager": is_manager,
                "link": link,
                "organisation": organisation.display_name if organisation else "Kxmeleon",
            },
        )


# ------------------------------------------------------------------ download


def open_for_download(db: Session, actor: Actor, file_id: uuid.UUID) -> Download:
    """Authorise view on a linked entity, then stream the clean object. Anything the
    actor may not see answers 404."""
    row = db.get(StoredFile, file_id)
    if row is None or row.status in {"deleted", "quarantined", "infected"}:
        raise NotFound()
    allowed = False
    for link in db.scalars(select(FileLink).where(FileLink.file_id == row.id)):
        purpose = get_purpose(link.purpose)
        if purpose is not None and _authorise(
            db, actor, purpose.view_permission, purpose, link.entity_id
        ):
            allowed = True
            break
    if not allowed:
        raise NotFound()
    if row.status == "pending_scan":
        raise Conflict("This file is still being checked for viruses. Try again in a minute.")
    purpose = get_purpose(row.purpose)
    if FieldClass(row.classification).masked_in_audit:
        record_by(db, actor, "files.file.download", entity_type="file", entity_id=str(row.id))
    return Download(
        filename=row.original_filename,
        mime=row.detected_mime,
        size=row.size_bytes,
        inline=bool(purpose and purpose.inline),
        chunks=storage.stream(storage.clean_bucket(), row.storage_key),
    )


# ------------------------------------------------------------------ queries


def current_file(
    db: Session, *, purpose_key: str, entity_id: str, clean_only: bool = False
) -> FileView | None:
    """The newest live version attached to an entity for a purpose (e.g. the logo).
    With `clean_only`, the newest one that may be shown (pending versions are skipped)."""
    row = db.scalars(
        select(StoredFile)
        .join(FileLink, FileLink.file_id == StoredFile.id)
        .where(
            FileLink.purpose == purpose_key,
            FileLink.entity_id == entity_id,
            StoredFile.status.in_(("clean",) if clean_only else ("clean", "pending_scan")),
        )
        .order_by(StoredFile.created_at.desc())
        .limit(1)
    ).first()
    return _view(db, row) if row else None


def group_of(db: Session, file_id: uuid.UUID) -> uuid.UUID | None:
    row = db.get(StoredFile, file_id)
    return row.version_group_id if row else None


def latest_versions(db: Session, group_ids: list[uuid.UUID]) -> dict[uuid.UUID, FileView]:
    """The newest version of each document that hasn't been deleted (any scan state, so
    owners can see a version waiting for its scan or quarantined)."""
    if not group_ids:
        return {}
    newest: dict[uuid.UUID, StoredFile] = {}
    for row in db.scalars(
        select(StoredFile)
        .where(StoredFile.version_group_id.in_(group_ids), StoredFile.status != "deleted")
        .order_by(StoredFile.version.desc())
    ):
        newest.setdefault(row.version_group_id, row)
    return {group: _view(db, row) for group, row in newest.items()}


def delete_group(db: Session, actor: Actor, group_id: uuid.UUID) -> None:
    """Soft-delete every live version of a document."""
    rows = list(
        db.scalars(
            select(StoredFile).where(
                StoredFile.version_group_id == group_id, StoredFile.status != "deleted"
            )
        )
    )
    if not rows:
        raise NotFound()
    for row in rows:
        delete(db, actor, row.id)


def usage(db: Session) -> Usage:
    settings = _settings_row(db)
    counts = dict(
        db.execute(select(StoredFile.status, func.count()).group_by(StoredFile.status)).all()
    )
    return Usage(
        settings.quota_bytes,
        settings.used_bytes,
        counts.get("pending_scan", 0),
        counts.get("quarantined", 0) + counts.get("infected", 0),
    )


def list_files(
    db: Session,
    actor: Actor,
    *,
    q: str = "",
    purpose: str = "",
    status: str = "",
    held: bool = False,
    page: int = 1,
) -> FilePage:
    _require(db, actor, "files.file.manage")
    where: list[ColumnElement[bool]] = []
    if q.strip():
        where.append(func.lower(StoredFile.original_filename).like(f"%{q.strip().lower()}%"))
    if purpose:
        where.append(StoredFile.purpose == purpose)
    if status == "quarantined":
        where.append(StoredFile.status.in_(("quarantined", "infected")))
    elif status:
        where.append(StoredFile.status == status)
    if held:
        where.append(StoredFile.legal_hold.is_(True))
    total = db.scalar(select(func.count()).select_from(StoredFile).where(*where)) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, page), pages)
    rows = list(
        db.scalars(
            select(StoredFile)
            .where(*where)
            .order_by(StoredFile.created_at.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        )
    )
    return FilePage([_view(db, r) for r in rows], total, page, pages)


def file_detail(db: Session, actor: Actor, file_id: uuid.UUID) -> FileView:
    _require(db, actor, "files.file.manage")
    row = db.get(StoredFile, file_id)
    if row is None:
        raise NotFound()
    return _view(db, row)


# ------------------------------------------------------------------ changes


def _require(db: Session, actor: Actor, permission: str) -> None:
    if not engine.check(db, actor.user_id, permission):
        raise PermissionDenied()


def delete(db: Session, actor: Actor, file_id: uuid.UUID) -> None:
    """Soft delete. The retention job removes the bytes after the purpose's retention
    period, unless the file is on legal hold."""
    row = db.get(StoredFile, file_id)
    if row is None or row.status == "deleted":
        raise NotFound()
    purpose = get_purpose(row.purpose)
    may_attach = purpose is not None and any(
        _authorise(db, actor, purpose.attach_permission, purpose, link.entity_id)
        for link in db.scalars(select(FileLink).where(FileLink.file_id == row.id))
    )
    if not may_attach and not engine.check(db, actor.user_id, "files.file.manage"):
        raise NotFound()
    if row.legal_hold:
        raise Conflict("This file is on legal hold and can't be deleted.")
    now = clock.now()
    row.status, row.deleted_at = "deleted", now
    if purpose is not None:
        row.retention_until = now + purpose.retention
    record_by(db, actor, "files.file.delete", entity_type="file", entity_id=str(row.id))


def delete_by_system(db: Session, file_id: uuid.UUID, reason: str) -> None:
    """Soft delete by a scheduled job (a module's retention rule). Files on legal hold
    are left alone."""
    row = db.get(StoredFile, file_id)
    if row is None or row.status == "deleted" or row.legal_hold:
        return
    now = clock.now()
    row.status, row.deleted_at = "deleted", now
    purpose = get_purpose(row.purpose)
    if purpose is not None:
        row.retention_until = now + purpose.retention
    record(
        db,
        AuditEvent(
            action="files.file.delete",
            actor_type="system",
            entity_type="file",
            entity_id=str(row.id),
            reason=reason,
        ),
    )


def set_legal_hold(db: Session, actor: Actor, file_id: uuid.UUID, *, on: bool, reason: str) -> None:
    _require(db, actor, "files.file.manage")
    row = db.get(StoredFile, file_id)
    if row is None:
        raise NotFound()
    if len(reason.strip()) < 5:
        raise ValidationFailed(field_errors={"reason": "Say why, in a few words."})
    if row.legal_hold != on:
        row.legal_hold = on
        record_by(
            db,
            actor,
            "files.file.legal_hold",
            entity_type="file",
            entity_id=str(row.id),
            reason=reason.strip(),
            changes={"legal_hold": {"from": not on, "to": on}},
        )


def set_quota(db: Session, actor: Actor, quota_bytes: int | None) -> None:
    _require(db, actor, "system.storage.manage")
    if quota_bytes is not None and quota_bytes <= 0:
        raise ValidationFailed(
            field_errors={"quota": "Enter a size above zero, or leave it blank."}
        )
    settings = _settings_row(db, lock=True)
    if settings.quota_bytes != quota_bytes:
        record_by(
            db,
            actor,
            "files.quota.update",
            entity_type="storage",
            changes={"quota_bytes": {"from": settings.quota_bytes, "to": quota_bytes}},
        )
        settings.quota_bytes = quota_bytes


def run_retention(now: datetime | None = None, limit: int = 500) -> int:
    """Hard-delete files past their retention date and not on legal hold. Nightly."""
    now = now or clock.now()
    removed = 0
    with session_scope() as db:
        rows = list(
            db.scalars(
                select(StoredFile)
                .where(
                    StoredFile.retention_until.is_not(None),
                    StoredFile.retention_until <= now,
                    StoredFile.legal_hold.is_(False),
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        settings = _settings_row(db, lock=True) if rows else None
        for row in rows:
            for bucket in (storage.quarantine_bucket(), storage.clean_bucket()):
                try:
                    storage.delete(bucket, row.storage_key)
                except Exception:  # noqa: BLE001  (already gone is fine)
                    log.warning("retention_delete_failed", extra={"file_id": str(row.id)})
            assert settings is not None  # noqa: S101
            settings.used_bytes = max(0, settings.used_bytes - row.size_bytes)
            record(
                db,
                AuditEvent(
                    action="files.file.purge",
                    actor_type="system",
                    entity_type="file",
                    entity_id=str(row.id),
                    changes={"name": {"from": row.original_filename, "to": None}},
                ),
            )
            db.delete(row)
            removed += 1
    return removed
