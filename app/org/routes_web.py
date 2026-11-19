"""Administration → Organisation: legal details and document colours."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.authz.declarations import requires, screen
from app.core.db import get_db
from app.core.errors import NotFound, ValidationFailed
from app.identity.schemas import Actor
from app.org import service as org
from app.org.schemas import BrandingInput, OrganizationInput
from app.web.access import require_actor
from app.web.forms import field_errors
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]

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
    context = _context(db, actor, notice="Organisation details saved." if notice else None)
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
