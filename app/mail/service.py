"""Mail: SMTP settings, the outbox and delivery (ADR 0012)."""

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import AuditEvent, record
from app.core import clock
from app.core.db import session_scope
from app.core.secrets import DecryptionFailed, decrypt, encrypt
from app.mail.models import MailOutbox, MailSettings
from app.mail.rendering import render
from app.mail.schemas import MailSettingsInput, MailSettingsView, OutboxStats, TestResult
from app.mail.transport import (
    FAILURE_MESSAGES,
    Attachment,
    Message,
    SendFailed,
    SmtpConfig,
    send,
)

log = logging.getLogger("app.mail")

MAX_ATTEMPTS = 5


def _row(db: Session) -> MailSettings | None:
    return db.scalars(select(MailSettings)).one_or_none()


def get_settings_view(db: Session) -> MailSettingsView | None:
    row = _row(db)
    if row is None:
        return None
    return MailSettingsView(
        host=row.host,
        port=row.port,
        security=row.security,
        username=row.username,
        has_password=row.password_encrypted is not None,
        from_name=row.from_name,
        from_address=row.from_address,
        reply_to=row.reply_to,
        last_test_at=row.last_test_at,
        last_test_ok=row.last_test_ok,
        last_test_to=row.last_test_to,
    )


def save_settings(
    db: Session, data: MailSettingsInput, *, actor_id: uuid.UUID | None, actor_label: str
) -> None:
    row = _row(db)
    before = (
        {}
        if row is None
        else {
            "host": row.host,
            "port": row.port,
            "security": row.security,
            "username": row.username,
            "from_address": row.from_address,
        }
    )
    if row is None:
        row = MailSettings(
            host=data.host,
            port=data.port,
            security=data.security,
            from_name=data.from_name,
            from_address=str(data.from_address),
        )
        db.add(row)
    row.host = data.host.strip()
    row.port = data.port
    row.security = data.security
    row.username = (data.username or "").strip() or None
    row.from_name = data.from_name.strip()
    row.from_address = str(data.from_address)
    row.reply_to = str(data.reply_to) if data.reply_to else None
    password_changed = bool(data.password)
    if data.password:
        row.password_encrypted = encrypt(data.password)
    after = {
        "host": row.host,
        "port": row.port,
        "security": row.security,
        "username": row.username,
        "from_address": row.from_address,
    }
    changes: dict[str, Any] = {
        k: {"from": before.get(k), "to": v} for k, v in after.items() if before.get(k) != v
    }
    if password_changed:
        changes["password"] = {"from": "••••", "to": "••••"}
    record(
        db,
        AuditEvent(
            action="settings.mail.update",
            actor_type="user",
            actor_id=actor_id,
            actor_label=actor_label,
            entity_type="mail_settings",
            changes=changes or None,
        ),
    )


def _config(row: MailSettings) -> SmtpConfig:
    password = None
    if row.password_encrypted:
        try:
            password = decrypt(row.password_encrypted)
        except DecryptionFailed:
            password = None
    return SmtpConfig(
        host=row.host,
        port=row.port,
        security=row.security,
        username=row.username,
        password=password,
        from_name=row.from_name,
        from_address=row.from_address,
        reply_to=row.reply_to,
    )


def send_test(
    db: Session,
    *,
    to: str,
    name: str,
    organisation: str,
    actor_id: uuid.UUID | None,
    actor_label: str,
) -> TestResult:
    """Send immediately (the only synchronous send) and record the outcome."""
    row = _row(db)
    if row is None:
        return TestResult(False, FAILURE_MESSAGES["not_configured"])
    subject, text, html = render("test", {"name": name, "organisation": organisation})
    try:
        send(_config(row), Message(to=to, subject=subject, text=text, html=html))
        result = TestResult(True, f"Test email sent to {to}.")
    except SendFailed as failure:
        result = TestResult(False, FAILURE_MESSAGES[failure.category])
    row.last_test_at = clock.now()
    row.last_test_ok = result.ok
    row.last_test_to = to
    record(
        db,
        AuditEvent(
            action="settings.mail.test",
            actor_type="user",
            actor_id=actor_id,
            actor_label=actor_label,
            outcome="success" if result.ok else "failure",
            entity_type="mail_settings",
        ),
    )
    return result


def is_configured(db: Session) -> bool:
    return _row(db) is not None


def queue(
    db: Session,
    *,
    to: str,
    template: str,
    context: dict[str, Any],
    attachment: Attachment | None = None,
) -> None:
    """Add an email to the outbox in the caller's transaction; a worker sends it."""
    subject, text, html = render(template, context)
    db.add(
        MailOutbox(
            to_address=to,
            subject=subject,
            body_text=text,
            body_html=html,
            template=template,
            attachment_name=attachment.filename if attachment else None,
            attachment_type=attachment.content_type if attachment else None,
            attachment_data=attachment.data if attachment else None,
        )
    )


def _attachment(item: MailOutbox) -> Attachment | None:
    if not (item.attachment_name and item.attachment_type and item.attachment_data):
        return None
    return Attachment(item.attachment_name, item.attachment_type, item.attachment_data)


def send_pending(limit: int = 50) -> int:
    """Deliver due outbox rows. Safe to run concurrently (SKIP LOCKED). Returns sent count."""
    sent = 0
    with session_scope() as db:
        row = _row(db)
        if row is None:
            return 0
        config = _config(row)
        due = db.scalars(
            select(MailOutbox)
            .where(MailOutbox.status == "pending", MailOutbox.next_attempt_at <= clock.now())
            .order_by(MailOutbox.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for item in due:
            item.attempts += 1
            try:
                send(
                    config,
                    Message(
                        item.to_address,
                        item.subject,
                        item.body_text,
                        item.body_html,
                        _attachment(item),
                    ),
                )
            except SendFailed as failure:
                item.last_error = failure.category
                if item.attempts >= MAX_ATTEMPTS:
                    item.status = "failed"
                else:
                    item.next_attempt_at = clock.now() + timedelta(minutes=2**item.attempts)
                log.warning(
                    "mail_send_failed",
                    extra={"outbox_id": str(item.id), "category": failure.category},
                )
                continue
            item.status = "sent"
            item.sent_at = clock.now()
            item.last_error = None
            sent += 1
    return sent


def outbox_stats(db: Session) -> OutboxStats:
    start_of_day = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
    counts = dict(
        db.execute(select(MailOutbox.status, func.count()).group_by(MailOutbox.status)).all()
    )
    sent_today = (
        db.scalar(
            select(func.count())
            .select_from(MailOutbox)
            .where(MailOutbox.status == "sent", MailOutbox.sent_at >= start_of_day)
        )
        or 0
    )
    return OutboxStats(
        sent_today=sent_today, waiting=counts.get("pending", 0), failed=counts.get("failed", 0)
    )
