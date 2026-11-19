"""The file service against Postgres and an emulated S3 (brief section 5.3): upload
rejections, scanning, authorised downloads, versions, deletion, retention, legal hold
and quota."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core import clock
from app.core.db import session_scope
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files import storage
from app.files.models import StorageSettings, StoredFile
from app.identity.models import User
from app.identity.schemas import Actor
from app.mail.models import MailOutbox
from app.org import service as org
from tests.conftest import Installed
from tests.files.conftest import EICAR, SAFE_SVG, EicarScanner, png

pytestmark = pytest.mark.db
LOGO = "organization_logo"


def _actor(user_id: object) -> Actor:
    with session_scope() as db:
        user = db.get(User, user_id)
        assert user is not None
        return Actor(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            mfa_enabled=True,
            mfa_required=True,
            mfa_satisfied=True,
            via="session",
        )


@pytest.fixture()
def admin(installed: Installed) -> Actor:
    return _actor(installed.admin_id)


@pytest.fixture()
def clerk(installed: Installed) -> Actor:
    """Signed in, no roles at all."""
    with session_scope() as db:
        user = User(email="clerk@adeyemiholdings.ng", full_name="Plain Clerk", status="active")
        db.add(user)
        db.flush()
        user_id = user.id
    return _actor(user_id)


def _org_id() -> str:
    with session_scope() as db:
        return str(org.organization_id(db))


def _upload(
    actor: Actor,
    data: bytes,
    *,
    name: str = "logo.png",
    purpose: str = LOGO,
    entity: str | None = None,
    replaces: uuid.UUID | None = None,
) -> uuid.UUID:
    with session_scope() as db:
        return files.upload(
            db,
            actor,
            purpose_key=purpose,
            entity_id=entity or _org_id(),
            filename=name,
            declared_mime="image/png",
            data=data,
            replaces=replaces,
        )


def _status(file_id: uuid.UUID) -> str:
    with session_scope() as db:
        row = db.get(StoredFile, file_id)
        assert row is not None
        return row.status


def _download(actor: Actor, file_id: uuid.UUID) -> bytes:
    with session_scope() as db:
        return b"".join(files.open_for_download(db, actor, file_id).chunks)


# ------------------------------------------------------------------ upload rejections


def test_an_upload_waits_in_quarantine_until_scanned(admin: Actor, scanner: EicarScanner) -> None:
    file_id = _upload(admin, png())
    assert _status(file_id) == "pending_scan"
    with session_scope() as db, pytest.raises(Conflict, match="being checked"):
        files.open_for_download(db, admin, file_id)
    assert files.scan_pending() == 1
    assert _status(file_id) == "clean"
    assert _download(admin, file_id) == png()


def test_oversized_files_are_refused(admin: Actor) -> None:
    with pytest.raises(ValidationFailed) as exc:
        _upload(admin, png() + b"\0" * (2 * 1024 * 1024))
    assert "too large" in exc.value.field_errors["file"]


def test_the_type_comes_from_the_content(admin: Actor) -> None:
    with pytest.raises(ValidationFailed) as exc:
        _upload(admin, b"%PDF-1.7 not an image", name="logo.png")
    assert "isn't accepted" in exc.value.field_errors["file"]


def test_unsafe_svgs_are_refused_and_safe_ones_accepted(admin: Actor) -> None:
    with pytest.raises(ValidationFailed):
        _upload(admin, b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', name="x.svg")
    assert _upload(admin, SAFE_SVG, name="logo.svg")


def test_unregistered_purposes_are_refused(admin: Actor) -> None:
    with pytest.raises(ValidationFailed, match="can't be attached"):
        _upload(admin, png(), purpose="expense_receipt")


def test_attaching_to_another_entity_is_not_found(admin: Actor) -> None:
    with pytest.raises(NotFound):
        _upload(admin, png(), entity=str(uuid.uuid4()))


def test_attaching_needs_the_purposes_permission(clerk: Actor) -> None:
    with pytest.raises(PermissionDenied):
        _upload(clerk, png())


def test_the_quota_is_enforced_and_usage_tracked(admin: Actor) -> None:
    with session_scope() as db:
        db.execute(update(StorageSettings).values(quota_bytes=len(png()) + 10))
    _upload(admin, png())
    with session_scope() as db:
        assert files.usage(db).used_bytes == len(png())
    with pytest.raises(Conflict, match="storage is full"):
        _upload(admin, png())


# ------------------------------------------------------------------ scanning


def test_infected_files_are_quarantined_audited_and_reported(
    admin: Actor, clerk: Actor, scanner: EicarScanner
) -> None:
    file_id = _upload(
        admin, SAFE_SVG.replace(b"</svg>", b"<!--" + EICAR + b"--></svg>"), name="logo.svg"
    )
    files.scan_pending()
    assert _status(file_id) == "quarantined"
    with session_scope() as db:
        assert db.get(StoredFile, file_id).scan_result == "Eicar-Test-Signature"  # type: ignore[union-attr]
        alerts = db.scalars(
            select(MailOutbox).where(MailOutbox.template == "file_quarantined")
        ).all()
        assert [a.to_address for a in alerts] == [admin.email]  # uploader is also the manager
        with pytest.raises(NotFound):
            files.open_for_download(db, admin, file_id)
    assert not storage.client().list_objects_v2(Bucket=storage.clean_bucket()).get("Contents")


def test_an_unreachable_scanner_leaves_files_pending(admin: Actor, scanner: EicarScanner) -> None:
    scanner.down = True
    file_id = _upload(admin, png())
    assert files.scan_pending() == 0
    assert _status(file_id) == "pending_scan"
    scanner.down = False
    assert files.scan_pending() == 1


# ------------------------------------------------------------------ downloads


def test_anyone_signed_in_may_view_the_logo(admin: Actor, clerk: Actor) -> None:
    file_id = _upload(admin, png())
    files.scan_pending()
    assert _download(clerk, file_id) == png()


def test_unknown_files_are_not_found(admin: Actor) -> None:
    with session_scope() as db, pytest.raises(NotFound):
        files.open_for_download(db, admin, uuid.uuid4())


# ------------------------------------------------------------------ versions, deletion, retention


def test_replacing_creates_a_version_and_keeps_the_old_one_readable(admin: Actor) -> None:
    first = _upload(admin, png(2, 2))
    second = _upload(admin, png(3, 3), replaces=first)
    files.scan_pending()
    with session_scope() as db:
        old, new = db.get(StoredFile, first), db.get(StoredFile, second)
        assert old and new and new.version == 2 and new.version_group_id == old.version_group_id
        assert old.retention_until is not None
        assert files.current_file(db, purpose_key=LOGO, entity_id=_org_id()).id == second  # type: ignore[union-attr]
    assert _download(admin, first) == png(2, 2)


def test_deleted_files_vanish_and_are_purged_after_retention(admin: Actor) -> None:
    file_id = _upload(admin, png())
    files.scan_pending()
    with session_scope() as db:
        files.delete(db, admin, file_id)
    with session_scope() as db, pytest.raises(NotFound):
        files.open_for_download(db, admin, file_id)
    assert files.run_retention() == 0  # not yet
    assert files.run_retention(clock.now() + timedelta(days=366)) == 1
    with session_scope() as db:
        assert db.get(StoredFile, file_id) is None
        assert files.usage(db).used_bytes == 0
    assert not storage.client().list_objects_v2(Bucket=storage.clean_bucket()).get("Contents")


def test_legal_hold_blocks_deletion_and_purging(admin: Actor) -> None:
    file_id = _upload(admin, png())
    with session_scope() as db:
        files.set_legal_hold(db, admin, file_id, on=True, reason="Pending tax audit")
    with session_scope() as db, pytest.raises(Conflict, match="legal hold"):
        files.delete(db, admin, file_id)
    with session_scope() as db:
        db.execute(update(StoredFile).values(retention_until=clock.now() - timedelta(days=1)))
    assert files.run_retention() == 0
    with session_scope() as db:
        files.set_legal_hold(db, admin, file_id, on=False, reason="Audit closed")
    assert files.run_retention() == 1


def test_file_administration_needs_its_permission(clerk: Actor) -> None:
    with session_scope() as db, pytest.raises(PermissionDenied):
        files.list_files(db, clerk)


def test_an_infected_replacement_leaves_the_current_version_in_place(
    admin: Actor, scanner: EicarScanner
) -> None:
    first = _upload(admin, png())
    files.scan_pending()
    bad = SAFE_SVG.replace(b"</svg>", b"<!--" + EICAR + b"--></svg>")
    _upload(admin, bad, name="logo.svg", replaces=first)
    files.scan_pending()
    with session_scope() as db:
        assert db.get(StoredFile, first).retention_until is None  # type: ignore[union-attr]
        shown = files.current_file(db, purpose_key=LOGO, entity_id=_org_id(), clean_only=True)
        assert shown is not None and shown.id == first
