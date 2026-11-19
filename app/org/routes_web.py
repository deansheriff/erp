"""Administration → Organisation: legal details and document colours."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.authz.declarations import requires, screen
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.org import service as org
from app.org.files import ORGANIZATION_LOGO
from app.org.schemas import BrandingInput, OrganizationInput
from app.web.access import require_actor
from app.web.forms import field_errors
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
LogoUpload = Annotated[ReceivedUpload, Depends(upload_body(ORGANIZATION_LOGO.max_bytes))]

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    current = org.get_organization(db)
    if current is None:
        raise NotFound()
    values = extra.pop("values", None) or {
        "legal_name": current.legal_name,
        "trading_name": current.trading_name or "",
        "rc_number": current.rc_number or "",
        "tin": current.tin or "",
        "address": current.address or "",
        "fiscal_year_start_month": current.fiscal_year_start_month,
        "brand_primary": current.brand_primary or "",
        "brand_accent": current.brand_accent or "",
    }
    return {
        "shell": build_shell(
            db,
            actor,
            active_module="admin",
            active_section="Organisation",
            crumbs=["Administration", "Organisation"],
        ),
        "current": current,
        "logo": files.current_file(
            db, purpose_key=ORGANIZATION_LOGO.key, entity_id=str(org.organization_id(db))
        ),
        "shown_logo": files.current_file(
            db,
            purpose_key=ORGANIZATION_LOGO.key,
            entity_id=str(org.organization_id(db)),
            clean_only=True,
        ),
        "logo_purpose": ORGANIZATION_LOGO,
        "logo_errors": {},
        "values": values,
        "errors": {},
        "months": MONTHS,
        "page_title": "Organisation",
        **extra,
    }


@router.get("/admin/organisation", response_class=HTMLResponse)
@requires("system.branding.manage")
@screen("admin.organisation")
def organisation(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    messages = {
        "saved": "Organisation details saved.",
        "logo": "Logo uploaded. It appears once the virus check finishes, usually within a minute.",
        "logo_removed": "Logo removed.",
    }
    context = _context(db, actor, notice=messages.get(notice))
    return templates.TemplateResponse(request, "org/organisation.html", context)


@router.post("/admin/organisation", response_class=HTMLResponse)
@requires("system.branding.manage")
@screen("admin.organisation")
def save_organisation(
    request: Request,
    db: Db,
    actor: CurrentActor,
    legal_name: FormStr = "",
    trading_name: FormStr = "",
    rc_number: FormStr = "",
    tin: FormStr = "",
    address: FormStr = "",
    fiscal_year_start_month: Annotated[int, Form()] = 1,
    brand_primary: FormStr = "",
    brand_accent: FormStr = "",
) -> Response:
    values: dict[str, Any] = {
        "legal_name": legal_name,
        "trading_name": trading_name,
        "rc_number": rc_number,
        "tin": tin,
        "address": address,
        "fiscal_year_start_month": fiscal_year_start_month,
        "brand_primary": brand_primary,
        "brand_accent": brand_accent,
    }
    current = org.get_organization(db)
    if current is None:
        raise NotFound()
    try:
        data = OrganizationInput(
            legal_name=legal_name,
            trading_name=trading_name,
            rc_number=rc_number,
            tin=tin,
            address=address,
            timezone=current.timezone,
            base_currency=current.base_currency,
            fiscal_year_start_month=fiscal_year_start_month,
        )
        branding = BrandingInput(
            brand_primary=brand_primary or None, brand_accent=brand_accent or None
        )
        org.save_details(db, actor, data, branding)
    except ValidationError as exc:
        return templates.TemplateResponse(
            request,
            "org/organisation.html",
            _context(db, actor, values=values, errors=field_errors(exc)),
            status_code=422,
        )
    except ValidationFailed as exc:
        return templates.TemplateResponse(
            request,
            "org/organisation.html",
            _context(db, actor, values=values, errors=exc.field_errors),
            status_code=422,
        )
    return RedirectResponse("/admin/organisation?notice=saved", status_code=303)


@router.post("/admin/organisation/logo", response_class=HTMLResponse)
@requires("system.branding.manage")
@screen("admin.organisation")
def upload_logo(request: Request, db: Db, actor: CurrentActor, upload: LogoUpload) -> Response:
    current = files.current_file(
        db, purpose_key=ORGANIZATION_LOGO.key, entity_id=str(org.organization_id(db))
    )
    try:
        if upload.errors:
            raise ValidationFailed(field_errors=upload.errors)
        files.upload(
            db,
            actor,
            purpose_key=ORGANIZATION_LOGO.key,
            entity_id=str(org.organization_id(db)),
            filename=upload.filename,
            declared_mime=upload.content_type,
            data=upload.data,
            replaces=current.id if current else None,
        )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = getattr(exc, "field_errors", None) or {"file": exc.message}
        return templates.TemplateResponse(
            request,
            "org/organisation.html",
            _context(db, actor, logo_errors=errors),
            status_code=422,
        )
    files.request_scan()
    return RedirectResponse("/admin/organisation?notice=logo", status_code=303)


@router.post("/admin/organisation/logo/remove", response_class=HTMLResponse)
@requires("system.branding.manage")
@screen("admin.organisation")
def remove_logo(db: Db, actor: CurrentActor) -> Response:
    current = files.current_file(
        db, purpose_key=ORGANIZATION_LOGO.key, entity_id=str(org.organization_id(db))
    )
    if current is None:
        raise NotFound()
    files.delete(db, actor, current.id)
    return RedirectResponse("/admin/organisation?notice=logo_removed", status_code=303)
