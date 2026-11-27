"""Assistant web routes (ADR 0035): policy questions, job description drafts, appraisal
evidence, the policy library, the person's history, the ticket panel on support tickets,
and the settings. Routes translate forms and render; the services do the work."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, requires_login, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.models import User
from app.identity.schemas import Actor
from app.modules.assistant import common, features, history, library, setup
from app.modules.assistant.files import POLICY_FILE
from app.modules.assistant.models import KINDS, PROVIDERS, AssistantRequest
from app.org import service as org
from app.web.access import require_actor
from app.web.forms import all_fields, parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
Fields = Annotated[dict[str, str], Depends(all_fields)]
FormList = Annotated[list[str], Form()]
PolicyUpload = Annotated[ReceivedUpload, Depends(upload_body(POLICY_FILE.max_bytes))]
DomainProblem = ValidationFailed | Conflict | PermissionDenied
NOTICES = {
    "policy": "Policy added. Its text is now used for answers.",
    "saved": "Settings saved.",
    "withdrawn": (
        "Settings saved. The provider or the data changed, so the assistant is off until "
        "you accept again."
    ),
    "on": "The assistant is on.",
    "off": "The assistant is off. Nothing more will be sent.",
}


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str) -> Any:
    return build_shell(
        db, actor, active_module="assistant", active_section=section, crumbs=["Assistant", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _result(db: Session, actor: Actor, request_id: str, feature: str) -> AssistantRequest | None:
    rid = library.common_uuid(request_id) if request_id else None
    if rid is None:
        return None
    try:
        row = history.get(db, actor, rid)
    except NotFound:
        return None
    return row if row.feature == feature else None


def _base(db: Session, actor: Actor, section: str, feature: str, **extra: Any) -> dict[str, Any]:
    row = common.settings(db)
    return {
        "shell": _shell(db, actor, section),
        "unavailable": common.available(db, feature),
        "used": common.used_this_month(db, actor.user_id),
        "cap": row.monthly_cap,
        "feature_labels": common.FEATURE_LABELS,
        "values": {},
        "errors": {},
        "notice": None,
        "result": None,
        "page_title": section,
        **extra,
    }


# ------------------------------------------------------------------ policy questions


def _ask_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return _base(
        db,
        actor,
        "Ask about policies",
        "policy",
        recent=history.mine(db, actor, feature="policy", limit=8),
        policies=sum(1 for r in library.library(db, actor) if r.current),
        **extra,
    )


@router.get("/assistant", response_class=HTMLResponse)
@requires("assistant.policy.ask")
@screen("assistant.home")
def ask_page(request: Request, db: Db, actor: CurrentActor, r: str = "") -> HTMLResponse:
    context = _ask_context(db, actor, result=_result(db, actor, r, "policy"))
    return _render(request, "assistant/ask.html", context)


@router.post("/assistant", response_class=HTMLResponse)
@requires("assistant.policy.ask")
@screen("assistant.home")
def ask(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    try:
        with db.begin_nested():
            rid = features.ask_policy(db, actor, f.get("question", ""))
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _ask_context(db, actor, values=f, errors=_errors(exc))
        return _render(request, "assistant/ask.html", context, 422)
    if rid is None:
        context = _ask_context(db, actor, values=f, nothing=True)
        return _render(request, "assistant/ask.html", context)
    return _see_other(f"/assistant?r={rid}")


# ------------------------------------------------------------------ job descriptions


def _hiring_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    common.require(db, actor, common.HIRING)
    return _base(
        db,
        actor,
        "Job descriptions",
        "hiring",
        positions=org.position_choices(db),
        recent=history.mine(db, actor, feature="hiring", limit=8),
        **extra,
    )


@router.get("/assistant/hiring", response_class=HTMLResponse)
@requires("assistant.hiring.draft")
@screen("assistant.hiring")
def hiring_page(request: Request, db: Db, actor: CurrentActor, r: str = "") -> HTMLResponse:
    context = _hiring_context(db, actor, result=_result(db, actor, r, "hiring"))
    return _render(request, "assistant/hiring.html", context)


@router.post("/assistant/hiring", response_class=HTMLResponse)
@requires("assistant.hiring.draft")
@screen("assistant.hiring")
def draft_job(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    try:
        with db.begin_nested():
            rid = features.draft_job(db, actor, f.get("position_id", ""), f.get("notes", ""))
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _hiring_context(db, actor, values=f, errors=_errors(exc))
        return _render(request, "assistant/hiring.html", context, 422)
    return _see_other(f"/assistant/hiring?r={rid}")


# ------------------------------------------------------------------ appraisal evidence


def _evidence_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    today = clock.today()
    return _base(
        db,
        actor,
        "Appraisal evidence",
        "evidence",
        people=features.evidence_people(db, actor),
        recent=history.mine(db, actor, feature="evidence", limit=8),
        defaults={"start": today.replace(month=1, day=1).isoformat(), "end": today.isoformat()},
        **extra,
    )


@router.get("/assistant/evidence", response_class=HTMLResponse)
@requires("assistant.evidence.summarise")
@screen("assistant.evidence")
def evidence_page(request: Request, db: Db, actor: CurrentActor, r: str = "") -> HTMLResponse:
    context = _evidence_context(db, actor, result=_result(db, actor, r, "evidence"))
    return _render(request, "assistant/evidence.html", context)


@router.post("/assistant/evidence", response_class=HTMLResponse)
@requires("assistant.evidence.summarise")
@screen("assistant.evidence")
def summarise(request: Request, db: Db, actor: CurrentActor, f: Fields) -> Response:
    try:
        with db.begin_nested():
            rid = features.summarise_evidence(
                db,
                actor,
                f.get("employee_id", ""),
                parse_day(f.get("start")),
                parse_day(f.get("end")),
            )
    except (ValidationFailed, Conflict, PermissionDenied, NotFound) as exc:
        problem = (
            exc
            if not isinstance(exc, NotFound)
            else ValidationFailed(field_errors={"employee_id": "Choose someone you may see."})
        )
        context = _evidence_context(db, actor, values=f, errors=_errors(problem))
        return _render(request, "assistant/evidence.html", context, 422)
    if rid is None:
        context = _evidence_context(db, actor, values=f, nothing=True)
        return _render(request, "assistant/evidence.html", context)
    return _see_other(f"/assistant/evidence?r={rid}")


# ------------------------------------------------------------------ the policy library


def _library_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor, "Policy library"),
        "rows": library.library(db, actor),
        "can_manage": common.can(db, actor.user_id, common.MANAGE_POLICIES),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Policy library",
        **extra,
    }


@router.get("/assistant/policies", response_class=HTMLResponse)
@requires("assistant.policy.ask")
@screen("assistant.policies")
def library_page(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _library_context(db, actor, notice=NOTICES.get(notice))
    return _render(request, "assistant/policies.html", context)


@router.post("/assistant/policies", response_class=HTMLResponse)
@requires("assistant.policy.manage")
@screen("assistant.policies")
def add_policy(request: Request, db: Db, actor: CurrentActor, upload: PolicyUpload) -> Response:
    f = upload.fields
    data = library.PolicyInput(
        f.get("policy_id", ""),
        f.get("title", ""),
        f.get("version", ""),
        parse_day(f.get("effective_on")),
    )
    try:
        if upload.errors:
            raise ValidationFailed(field_errors=upload.errors)
        with db.begin_nested():
            library.add_version(
                db,
                actor,
                data,
                filename=upload.filename,
                mime=upload.content_type,
                content=upload.data,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _library_context(db, actor, values=f, errors=_errors(exc))
        return _render(request, "assistant/policies.html", context, 422)
    files.request_scan()
    return _see_other("/assistant/policies?notice=policy")


# ------------------------------------------------------------------ history


@router.get("/assistant/history", response_class=HTMLResponse)
@requires("assistant.policy.ask")
@screen("assistant.history")
def history_page(request: Request, db: Db, actor: CurrentActor, feature: str = "") -> HTMLResponse:
    context = {
        "shell": _shell(db, actor, "History"),
        "rows": history.mine(db, actor, feature=feature, limit=200),
        "feature": feature,
        "feature_labels": common.FEATURE_LABELS,
        "days": common.settings(db).history_days,
        "page_title": "Assistant history",
    }
    return _render(request, "assistant/history.html", context)


@router.get("/assistant/history/{request_id:uuid}", response_class=HTMLResponse)
@requires_login
@screen("assistant.history")
def history_item(db: Db, actor: CurrentActor, request_id: uuid.UUID) -> Response:
    row = history.get(db, actor, request_id)
    pages = {
        "policy": "/assistant",
        "hiring": "/assistant/hiring",
        "evidence": "/assistant/evidence",
    }
    if row.feature == "ticket":
        return _see_other(f"/support/{row.inputs.get('ticket_id', '')}")
    return _see_other(f"{pages[row.feature]}?r={row.id}")


# ------------------------------------------------------------------ the ticket panel


def _panel(db: Session, actor: Actor, ticket_id: uuid.UUID, **extra: Any) -> dict[str, Any] | None:
    """None when the actor can't use ticket suggestions here."""
    if not common.can(db, actor.user_id, common.TICKETS):
        return None
    return {
        "ticket_id": ticket_id,
        "unavailable": common.available(db, "ticket"),
        "result": None,
        "error": None,
        **extra,
    }


@router.get("/assistant/tickets/{ticket_id:uuid}/panel", response_class=HTMLResponse)
@requires_login
@screen("support.ticket")
def ticket_panel(request: Request, db: Db, actor: CurrentActor, ticket_id: uuid.UUID) -> Response:
    context = _panel(db, actor, ticket_id)
    if context is None:
        return HTMLResponse("")
    return _render(request, "assistant/_ticket_panel.html", context)


@router.post("/assistant/tickets/{ticket_id:uuid}/suggest", response_class=HTMLResponse)
@requires("assistant.ticket.suggest")
@screen("support.ticket")
def suggest(request: Request, db: Db, actor: CurrentActor, ticket_id: uuid.UUID) -> Response:
    try:
        with db.begin_nested():
            rid = features.suggest_for_ticket(db, actor, ticket_id)
        result = history.get(db, actor, rid)
        context = _panel(db, actor, ticket_id, result=result)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _panel(db, actor, ticket_id, error=_errors(exc).get("form") or exc.message)
    if context is None:
        raise PermissionDenied()
    return _render(request, "assistant/_ticket_panel.html", context)


# ------------------------------------------------------------------ settings


def _settings_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    common.require(db, actor, common.SETTINGS)
    row = common.settings(db)
    return {
        "shell": _shell(db, actor, "Settings"),
        "s": row,
        "providers": PROVIDERS,
        "provider_labels": common.PROVIDER_LABELS,
        "default_models": common.DEFAULT_MODELS,
        "kinds": KINDS,
        "kind_labels": common.KIND_LABELS,
        "kind_hints": common.KIND_HINTS,
        "usage": setup.usage(db, actor),
        "feature_labels": common.FEATURE_LABELS,
        "accepted_by": common_name(db, row.accepted_by),
        "accepted_labels": [common.KIND_LABELS[k] for k in row.accepted_kinds or []],
        "values": {
            "provider": row.provider,
            "model": row.model,
            "kinds": list(row.kinds),
            "monthly_cap": str(row.monthly_cap),
            "history_days": str(row.history_days),
        },
        "errors": {},
        "notice": None,
        "test_result": None,
        "page_title": "Assistant settings",
        **extra,
    }


def common_name(db: Session, user_id: uuid.UUID | None) -> str | None:
    user = db.get(User, user_id) if user_id else None
    return user.full_name if user else None


@router.get("/assistant/settings", response_class=HTMLResponse)
@requires("system.ai.manage")
@screen("assistant.settings")
def settings_page(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    context = _settings_context(db, actor, notice=NOTICES.get(notice))
    return _render(request, "assistant/settings.html", context)


def _settings_run(
    db: Session, actor: Actor, action: str, f: dict[str, str], kinds: list[str]
) -> str:
    """One settings action; returns the notice key (or, for a test, the message)."""
    if action == "save":
        data = setup.SettingsInput(
            f.get("provider", ""),
            f.get("model", ""),
            f.get("api_key", ""),
            kinds,
            f.get("monthly_cap", ""),
            f.get("history_days", ""),
        )
        return "withdrawn" if setup.save(db, actor, data) else "saved"
    if action == "on":
        setup.turn_on(db, actor, accepted=bool(f.get("accept")))
        return "on"
    if action == "off":
        setup.turn_off(db, actor)
        return "off"
    if action == "test":
        return setup.test_connection(db, actor)
    raise NotFound()


@router.post("/assistant/settings/{action}", response_class=HTMLResponse)
@requires("system.ai.manage")
@screen("assistant.settings")
def settings_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    action: str,
    f: Fields,
    kind: FormList = [],  # noqa: B006  (FastAPI form lists)
) -> Response:
    """save, on, off or test."""
    try:
        with db.begin_nested():
            notice = _settings_run(db, actor, action, f, kind)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _settings_context(db, actor, values={**f, "kinds": kind}, errors=_errors(exc))
        return _render(request, "assistant/settings.html", context, 422)
    if action == "test":
        return _render(
            request, "assistant/settings.html", _settings_context(db, actor, test_result=notice)
        )
    return _see_other(f"/assistant/settings?notice={notice}")
