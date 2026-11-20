"""Payroll self-service (ADR 0021): my payslips once paid, and declaring rent for rent
relief with proof."""

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.hr import service as people
from app.modules.payroll import reliefs, service
from app.modules.payroll.files import RENT_PROOF
from app.web.access import require_actor
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
ProofUpload = Annotated[ReceivedUpload, Depends(upload_body(RENT_PROOF.max_bytes))]

DECLARATION_TONES = {"pending": "warning", "verified": "success", "rejected": "danger"}


def _context(db: Session, actor: Actor, selected: str = "", **extra: Any) -> dict[str, Any]:
    slips = service.my_payslips(db, actor)
    chosen = next((s for s in slips if str(s.id) == selected), slips[0] if slips else None)
    year = clock.today().year
    ytd = [s for s in slips if s.period.endswith(str(year))]
    return {
        "shell": build_shell(
            db,
            actor,
            active_module="payroll",
            active_section="My payslips",
            crumbs=["Payroll", "My payslips"],
        ),
        "linked": people.employee_of_user(db, actor.user_id) is not None,
        "slips": slips,
        "selected": chosen,
        "ytd": {
            "gross": sum((s.gross for s in ytd), Decimal(0)),
            "paye": chosen.ytd_paye if chosen else Decimal(0),
            "pension": sum((s.pension_employee for s in ytd), Decimal(0)),
        },
        "declarations": reliefs.mine(db, actor),
        "years": [year, year - 1],
        "tones": DECLARATION_TONES,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "My payslips",
        **extra,
    }


@router.get("/payslips", response_class=HTMLResponse)
@requires("payroll.payslip.read")
@screen("payroll.my")
def my_payslips(
    request: Request, db: Db, actor: CurrentActor, selected: str = "", notice: str = ""
) -> HTMLResponse:
    notice_text = (
        "Rent declared. It counts for PAYE once HR or Payroll verifies it." if notice else None
    )
    context = _context(db, actor, selected, notice=notice_text)
    return templates.TemplateResponse(request, "payroll/my.html", context)


@router.post("/payslips/rent", response_class=HTMLResponse)
@requires("payroll.relief.declare")
@screen("payroll.my")
def declare_rent(request: Request, db: Db, actor: CurrentActor, upload: ProofUpload) -> Response:
    fields = upload.fields
    try:
        year = int(fields.get("tax_year", "0"))
    except ValueError:
        year = 0
    try:
        if upload.errors and upload.data:
            raise ValidationFailed(field_errors=upload.errors)
        with db.begin_nested():
            reliefs.declare(
                db,
                actor,
                tax_year=year,
                annual_rent=fields.get("annual_rent", ""),
                filename=upload.filename,
                declared_mime=upload.content_type,
                data=upload.data,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        errors = getattr(exc, "field_errors", None) or {"form": exc.message}
        context = _context(db, actor, values=dict(fields), errors=errors)
        return templates.TemplateResponse(request, "payroll/my.html", context, status_code=422)
    files.request_scan()
    return RedirectResponse("/payslips?notice=1#rent", status_code=303)
