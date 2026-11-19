"""Employee documents (ADR 0017): stored through the files service, one purpose per field
class, so who may attach and view a document follows the class of its category."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import diff_changes, record_by
from app.authz import engine
from app.authz import service as authz
from app.authz.fields import FieldClass, read_permission, update_permission
from app.core import clock
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.hr.files import DOCUMENT_PURPOSES
from app.modules.hr.models import DOCUMENT_CLASSES, DocumentCategory, EmployeeDocument
from app.modules.hr.records import PREFIX, resource
from app.modules.hr.schemas import CategoryView, DocumentView


def categories(db: Session, *, active_only: bool = False) -> list[CategoryView]:
    counts = dict(
        db.execute(
            select(EmployeeDocument.category_id, func.count())
            .where(EmployeeDocument.deleted_at.is_(None))
            .group_by(EmployeeDocument.category_id)
        ).all()
    )
    stmt = select(DocumentCategory).order_by(DocumentCategory.name)
    if active_only:
        stmt = stmt.where(DocumentCategory.is_active)
    return [
        CategoryView(c.id, c.name, c.field_class, c.is_active, counts.get(c.id, 0))
        for c in db.scalars(stmt)
    ]


def uploadable_categories(db: Session, actor: Actor, employee_id: uuid.UUID) -> list[CategoryView]:
    """Active categories whose class the actor may attach on this employee."""
    res = resource(employee_id)
    allowed = {
        c
        for c in DOCUMENT_CLASSES
        if engine.check(db, actor.user_id, update_permission(PREFIX, FieldClass(c)), res)
    }
    return [c for c in categories(db, active_only=True) if c.field_class in allowed]


def save_category(
    db: Session,
    actor: Actor,
    category_id: uuid.UUID | None,
    *,
    name: str,
    field_class: str,
    is_active: bool = True,
) -> uuid.UUID:
    authz.require(db, actor, "hr.settings.manage")
    name = " ".join(name.split())
    errors = {}
    if not 2 <= len(name) <= 80:
        errors["name"] = "Give the category a name."
    if field_class not in DOCUMENT_CLASSES:
        errors["field_class"] = "Choose who may see these documents."
    taken = db.scalar(
        select(DocumentCategory.id).where(func.lower(DocumentCategory.name) == name.lower())
    )
    if taken is not None and taken != category_id:
        errors["name"] = "There is already a category with this name."
    if errors:
        raise ValidationFailed(field_errors=errors)
    if category_id is None:
        row = DocumentCategory(name=name, field_class=field_class, is_active=is_active)
        db.add(row)
        db.flush()
        before: dict[str, object] = {}
    else:
        found = db.get(DocumentCategory, category_id)
        if found is None:
            raise NotFound()
        row = found
        before = {"name": row.name, "field_class": row.field_class, "is_active": row.is_active}
        # Existing documents keep the class they were filed under.
        row.name, row.field_class, row.is_active = name, field_class, is_active
    record_by(
        db,
        actor,
        "hr.document_category.save",
        entity_type="document_category",
        entity_id=str(row.id),
        changes=diff_changes(
            before, {"name": row.name, "field_class": row.field_class, "is_active": row.is_active}
        ),
    )
    return row.id


def list_documents(db: Session, actor: Actor, employee_id: uuid.UUID) -> list[DocumentView]:
    """Documents of the classes the actor may read on this employee."""
    people.get_person(db, actor, employee_id)
    res = resource(employee_id)
    readable = {
        c
        for c in DOCUMENT_CLASSES
        if engine.check(db, actor.user_id, read_permission(PREFIX, FieldClass(c)), res)
    }
    updatable = {
        c
        for c in DOCUMENT_CLASSES
        if engine.check(db, actor.user_id, update_permission(PREFIX, FieldClass(c)), res)
    }
    rows = list(
        db.scalars(
            select(EmployeeDocument)
            .where(
                EmployeeDocument.employee_id == employee_id,
                EmployeeDocument.deleted_at.is_(None),
                EmployeeDocument.field_class.in_(readable),
            )
            .order_by(EmployeeDocument.created_at.desc())
        )
    )
    names = {c.id: c.name for c in db.scalars(select(DocumentCategory))}
    latest = files.latest_versions(db, [r.version_group_id for r in rows])
    return [
        DocumentView(
            id=r.id,
            title=r.title,
            category=names.get(r.category_id, ""),
            field_class=r.field_class,
            created_at=r.created_at,
            file=latest.get(r.version_group_id),
            can_change=r.field_class in updatable,
        )
        for r in rows
    ]


def add_document(
    db: Session,
    actor: Actor,
    employee_id: uuid.UUID,
    *,
    category_id: uuid.UUID | None,
    title: str,
    filename: str | None,
    declared_mime: str | None,
    data: bytes,
) -> uuid.UUID:
    people.get_person(db, actor, employee_id)
    category = db.get(DocumentCategory, category_id) if category_id else None
    if category is None or not category.is_active:
        raise ValidationFailed(field_errors={"category_id": "Choose a category."})
    title = " ".join(title.split()) or (filename or category.name)
    if len(title) > 160:
        raise ValidationFailed(field_errors={"title": "Keep the title under 160 characters."})
    purpose = DOCUMENT_PURPOSES[category.field_class]
    file_id = files.upload(
        db,
        actor,
        purpose_key=purpose.key,
        entity_id=str(employee_id),
        filename=filename,
        declared_mime=declared_mime,
        data=data,
    )
    group_id = files.group_of(db, file_id)
    if group_id is None:
        raise NotFound()
    row = EmployeeDocument(
        employee_id=employee_id,
        category_id=category.id,
        title=title[:160],
        field_class=category.field_class,
        version_group_id=group_id,
        added_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    record_by(
        db,
        actor,
        "hr.document.add",
        entity_type="employee",
        entity_id=str(employee_id),
        changes={"document": {"from": None, "to": f"{category.name}: {row.title}"}},
    )
    return row.id


def employee_of(db: Session, document_id: uuid.UUID) -> uuid.UUID | None:
    return db.scalar(select(EmployeeDocument.employee_id).where(EmployeeDocument.id == document_id))


def _document(db: Session, actor: Actor, document_id: uuid.UUID) -> EmployeeDocument:
    row = db.get(EmployeeDocument, document_id)
    if row is None or row.deleted_at is not None:
        raise NotFound()
    people.get_person(db, actor, row.employee_id)
    permission = update_permission(PREFIX, FieldClass(row.field_class))
    if not engine.check(db, actor.user_id, permission, resource(row.employee_id)):
        read = read_permission(PREFIX, FieldClass(row.field_class))
        if engine.check(db, actor.user_id, read, resource(row.employee_id)):
            raise PermissionDenied()
        raise NotFound()
    return row


def replace_document(
    db: Session,
    actor: Actor,
    document_id: uuid.UUID,
    *,
    filename: str | None,
    declared_mime: str | None,
    data: bytes,
) -> uuid.UUID:
    row = _document(db, actor, document_id)
    current = files.latest_versions(db, [row.version_group_id]).get(row.version_group_id)
    if current is None:
        raise Conflict("This document has no file to replace.")
    file_id = files.upload(
        db,
        actor,
        purpose_key=DOCUMENT_PURPOSES[row.field_class].key,
        entity_id=str(row.employee_id),
        filename=filename,
        declared_mime=declared_mime,
        data=data,
        replaces=current.id,
    )
    record_by(
        db,
        actor,
        "hr.document.replace",
        entity_type="employee",
        entity_id=str(row.employee_id),
        changes={"document": {"from": row.title, "to": row.title}},
    )
    return file_id


def delete_document(db: Session, actor: Actor, document_id: uuid.UUID) -> uuid.UUID:
    row = _document(db, actor, document_id)
    files.delete_group(db, actor, row.version_group_id)
    row.deleted_at = clock.now()
    record_by(
        db,
        actor,
        "hr.document.delete",
        entity_type="employee",
        entity_id=str(row.employee_id),
        changes={"document": {"from": row.title, "to": None}},
    )
    return row.employee_id
