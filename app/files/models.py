"""File records, links to the entities they belong to, and the storage quota (ADR 0016)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

FILE_STATUSES = ("pending_scan", "clean", "infected", "quarantined", "deleted")
CLASSIFICATIONS = ("directory", "personal", "compensation", "banking", "statutory", "sensitive")


class StoredFile(IdMixin, Base):
    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_scan', 'clean', 'infected', 'quarantined', 'deleted')",
            name="status",
        ),
        CheckConstraint(
            "classification IN ('directory', 'personal', 'compensation', 'banking', "
            "'statutory', 'sensitive')",
            name="classification",
        ),
        CheckConstraint("size_bytes >= 0", name="size"),
        UniqueConstraint("version_group_id", "version", name="uq_files_version"),
        Index("ix_files_status", "status"),
        Index("ix_files_retention_until", "retention_until"),
    )

    # Random and opaque: never derived from the file name.
    storage_key: Mapped[str] = mapped_column(String(64), unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))  # sanitised; display only
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    declared_mime: Mapped[str | None] = mapped_column(String(127))
    detected_mime: Mapped[str] = mapped_column(String(127))
    status: Mapped[str] = mapped_column(String(16), server_default="pending_scan")
    classification: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(60))
    scan_result: Mapped[str | None] = mapped_column(Text)  # e.g. the signature found
    scanned_at: Mapped[datetime | None]
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[datetime | None]
    retention_until: Mapped[datetime | None]
    legal_hold: Mapped[bool] = mapped_column(Boolean, server_default="false")
    version_group_id: Mapped[uuid.UUID]
    version: Mapped[int] = mapped_column(Integer, server_default="1")


class FileLink(IdMixin, Base):
    __tablename__ = "file_links"
    __table_args__ = (
        UniqueConstraint("file_id", "entity_type", "entity_id", "purpose", name="uq_file_links"),
        Index("ix_file_links_entity", "entity_type", "entity_id"),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(60))
    linked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    linked_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StorageSettings(IdMixin, Base):
    """The organisation's quota and usage. One row."""

    __tablename__ = "storage_settings"
    __table_args__ = (
        CheckConstraint("is_singleton", name="one_row"),
        UniqueConstraint("is_singleton", name="uq_storage_settings_is_singleton"),
        CheckConstraint("used_bytes >= 0", name="used"),
    )

    is_singleton: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # 50 GB by default (confirmed 2026-11-19); editable. NULL would mean "no limit".
    quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    used_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
