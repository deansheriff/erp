"""File purposes for attendance (ADR 0019): the photo taken at clock-in. Only the person
and HR can open it (managers see that one was taken), and it is deleted 90 days after it
was taken (confirm with the product owner)."""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.attendance.models import AttendanceRecord


def _record(db: Session, entity_id: str) -> Resource | None:
    try:
        record = db.get(AttendanceRecord, uuid.UUID(entity_id))
    except ValueError:
        return None
    return Resource(owner_employee_id=record.employee_id) if record else None


PHOTO_DAYS = 90

ATTENDANCE_PHOTO = FilePurpose(
    key="attendance_photo",
    label="Clock-in photo",
    entity_type="attendance_record",
    allowed_types=frozenset({"image/png", "image/jpeg", "image/webp"}),
    max_bytes=8 * MB,
    attach_permission="attendance.clock.use",
    view_permission="attendance.photo.view",
    retention=timedelta(0),
    inline=True,
    classification="sensitive",
    resolve=_record,
    expires_after=timedelta(days=PHOTO_DAYS),
)

PURPOSES = (ATTENDANCE_PHOTO,)
