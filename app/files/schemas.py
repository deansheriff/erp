import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class LinkView:
    entity_type: str
    entity_id: str
    purpose: str


@dataclass(frozen=True, slots=True)
class FileView:
    id: uuid.UUID
    original_filename: str
    size_bytes: int
    sha256: str
    declared_mime: str | None
    detected_mime: str
    status: str
    classification: str
    purpose: str
    purpose_label: str
    uploaded_by: str | None
    created_at: datetime
    scanned_at: datetime | None
    scan_result: str | None
    deleted_at: datetime | None
    retention_until: datetime | None
    legal_hold: bool
    version: int
    version_count: int
    links: list[LinkView] = field(default_factory=list)

    @property
    def is_image(self) -> bool:
        return self.detected_mime.startswith("image/")


@dataclass(frozen=True, slots=True)
class Download:
    filename: str
    mime: str
    size: int
    inline: bool
    chunks: Iterator[bytes]


@dataclass(frozen=True, slots=True)
class Usage:
    quota_bytes: int | None
    used_bytes: int
    pending: int
    quarantined: int

    @property
    def percent(self) -> int:
        if not self.quota_bytes:
            return 0
        return min(100, round(100 * self.used_bytes / self.quota_bytes))


@dataclass(frozen=True, slots=True)
class FilePage:
    rows: list[FileView]
    total: int
    page: int
    pages: int
