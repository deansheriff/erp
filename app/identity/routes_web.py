"""Web routes for setup, sign-in, two-step verification, password reset, invitations,
my security and user administration. Routes are thin: parse, call one service, render."""

import uuid
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import pending_login, public, requires, requires_login, screen
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.identity import service as identity
from app.identity.schemas import Actor, AdminAccountInput, InviteInput
from app.mail import service as mail
from app.mail.schemas import MailSettingsInput
from app.org import service as org
from app.org.schemas import OrganizationInput
from app.web.access import (
    SESSION_COOKIE,
    SETUP_COOKIE,
    client_ip,
    current_actor,
    require_actor,
    safe_next,
)
from app.web.forms import field_errors
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
OptionalFormStr = Annotated[str | None, Form()]

MFA_SETUP_COOKIE = "kx_setup_mfa"

BAD_SETUP_CODE = "That setup code isn't valid or has expired. Run `kx setup-token` for a new one."
EXPIRED_INVITE = (
    "This invitation has expired or has already been used. Ask your administrator to resend it."
)


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _set_session_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        httponly=True,
        samesite="lax",
        secure=get_settings().secure_cookies,
        path="/",
    )


def _signed_in_redirect(db: Session, request: Request, user_id: uuid.UUID, target: str) -> Response:
    """Start a session after a successful password, then go to MFA if needed."""
    needs_mfa = identity.session_needs_mfa(db, user_id)
    raw = identity.create_session(
        db,
        user_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        mfa_satisfied=not needs_mfa,
    )
    destination = target
    if needs_mfa:
        destination = f"/login/verify?next={quote(target, safe='/')}"
    response = RedirectResponse(destination, status_code=303)
    _set_session_cookie(response, raw)
    return response


# ------------------------------------------------------------------ setup wizard


def _setup_context(db: Session, request: Request, **extra: Any) -> dict[str, Any]:
    organisation = org.get_organization(db)
    actor = current_actor(request)
    return {
        "step": identity.setup_progress(db),
        "can_finish": actor is not None
        and bool(engine.check(db, actor.user_id, "system.mail.manage")),
        "organisation": organisation,
        "page_title": "Set up Kxmeleon",
        **extra,
    }


@router.get("/setup", response_class=HTMLResponse)
@public
@screen("setup.wizard")
def setup_page(request: Request, db: Db) -> HTMLResponse:
    step = identity.setup_progress(db)
    token_ok = identity.check_setup_token(db, request.cookies.get(SETUP_COOKIE))
    context = _setup_context(db, request, token_ok=token_ok, values={}, errors={})
    if step == 2 and token_ok:
        secret = request.cookies.get(MFA_SETUP_COOKIE) or identity.setup_mfa_secret()
        context["enrolment"] = identity.setup_mfa_enrolment(
            secret, "", context["organisation"].display_name
        )
        response = _render(request, "identity/setup.html", context)
        response.set_cookie(
            MFA_SETUP_COOKIE,
            secret,
            httponly=True,
            samesite="strict",
            secure=get_settings().secure_cookies,
            max_age=3600,
            path="/setup",
        )
        return response
    return _render(request, "identity/setup.html", context)


@router.post("/setup/organisation", response_class=HTMLResponse)
@public
@screen("setup.wizard")
def setup_organisation(
    request: Request,
    db: Db,
    setup_code: OptionalFormStr = None,
    legal_name: FormStr = "",
    trading_name: OptionalFormStr = None,
    rc_number: OptionalFormStr = None,
    tin: OptionalFormStr = None,
    address: OptionalFormStr = None,
    timezone: FormStr = "Africa/Lagos",
    base_currency: FormStr = "NGN",
    fiscal_year_start_month: Annotated[int, Form()] = 1,
) -> Response:
    code = setup_code or request.cookies.get(SETUP_COOKIE)
    values = {
        "legal_name": legal_name,
        "trading_name": trading_name,
        "rc_number": rc_number,
        "tin": tin,
        "address": address,
        "timezone": timezone,
        "base_currency": base_currency,
        "fiscal_year_start_month": fiscal_year_start_month,
    }
    if not identity.check_setup_token(db, code):
        context = _setup_context(
            db,
            request,
            token_ok=False,
            values=values,
            errors={"setup_code": BAD_SETUP_CODE},
        )
        return _render(request, "identity/setup.html", context, 422)
    try:
        data = OrganizationInput(**values)
    except ValidationError as exc:
        context = _setup_context(
            db, request, token_ok=True, values=values, errors=field_errors(exc)
        )
        return _render(request, "identity/setup.html", context, 422)
    org.save_organization(db, data)
    response = RedirectResponse("/setup", status_code=303)
    response.set_cookie(
        SETUP_COOKIE,
        (code or "").strip().upper(),
        httponly=True,
        samesite="strict",
        secure=get_settings().secure_cookies,
        max_age=3600,
        path="/setup",
    )
    return response


@router.post("/setup/code", response_class=HTMLResponse)
@public
@screen("setup.wizard")
def setup_code(request: Request, db: Db, setup_code: FormStr = "") -> Response:
    """Re-enter a setup code part-way through (codes are re-issued on every restart)."""
    code = setup_code.strip().upper()
    if not identity.check_setup_token(db, code):
        context = _setup_context(
            db,
            request,
            token_ok=False,
            values={},
            errors={"setup_code": BAD_SETUP_CODE},
        )
        return _render(request, "identity/setup.html", context, 422)
    response = RedirectResponse("/setup", status_code=303)
    response.set_cookie(
        SETUP_COOKIE,
        code,
        httponly=True,
        samesite="strict",
        secure=get_settings().secure_cookies,
        max_age=3600,
        path="/setup",
    )
    return response


@router.post("/setup/admin", response_class=HTMLResponse)
@public
@screen("setup.wizard")
def setup_admin(
    request: Request,
    db: Db,
    full_name: FormStr = "",
    email: FormStr = "",
    password: FormStr = "",
    password_confirm: FormStr = "",
    mfa_code: FormStr = "",
) -> Response:
    if not identity.check_setup_token(db, request.cookies.get(SETUP_COOKIE)):
        raise NotFound()
    secret = request.cookies.get(MFA_SETUP_COOKIE)
    values = {"full_name": full_name, "email": email}
    organisation = org.get_organization(db)
    try:
        if not secret:
            raise ValidationFailed(field_errors={"mfa_code": "Scan the code again: it expired."})
        data = AdminAccountInput(
            full_name=full_name,
            email=email,
            password=password,
            password_confirm=password_confirm,
            mfa_code=mfa_code,
        )
        user_id, codes = identity.setup_create_super_admin(db, data, secret)
    except ValidationError as exc:
        errors = field_errors(exc)
    except ValidationFailed as exc:
        errors = exc.field_errors
    else:
        raw = identity.create_session(
            db,
            user_id,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            mfa_satisfied=True,
        )
        response = _render(
            request,
            "identity/recovery_codes.html",
            {"codes": codes, "continue_to": "/setup", "page_title": "Save your recovery codes"},
        )
        _set_session_cookie(response, raw)
        response.delete_cookie(MFA_SETUP_COOKIE, path="/setup")
        return response
    context = _setup_context(db, request, token_ok=True, values=values, errors=errors)
    context["enrolment"] = identity.setup_mfa_enrolment(
        secret or identity.setup_mfa_secret(),
        email,
        organisation.display_name if organisation else "",
    )
    return _render(request, "identity/setup.html", context, 422)


@router.post("/setup/mail", response_class=HTMLResponse)
@requires("system.mail.manage")
@screen("setup.wizard")
def setup_mail(
    request: Request,
    db: Db,
    actor: CurrentActor,
    action: FormStr = "save",
    host: FormStr = "",
    port: Annotated[int, Form()] = 587,
    security: FormStr = "starttls",
    username: OptionalFormStr = None,
    password: OptionalFormStr = None,
    from_name: FormStr = "",
    from_address: FormStr = "",
    reply_to: OptionalFormStr = None,
) -> Response:
    if not identity.check_setup_token(db, request.cookies.get(SETUP_COOKIE)):
        raise NotFound()
    if action == "skip":
        identity.complete_setup(db, actor)
        return _finish_setup()
    values = {
        "host": host,
        "port": port,
        "security": security,
        "username": username,
        "from_name": from_name,
        "from_address": from_address,
        "reply_to": reply_to or None,
    }
    try:
        data = MailSettingsInput(**values, password=password)
    except ValidationError as exc:
        context = _setup_context(
            db, request, token_ok=True, values=values, errors=field_errors(exc)
        )
        return _render(request, "identity/setup.html", context, 422)
    mail.save_settings(db, data, actor_id=actor.user_id, actor_label=actor.label)
    if action == "test":
        organisation = org.get_organization(db)
        result = mail.send_test(
            db,
            to=actor.email,
            name=actor.full_name.split()[0],
            organisation=organisation.display_name if organisation else "",
            actor_id=actor.user_id,
            actor_label=actor.label,
        )
        context = _setup_context(
            db,
            request,
            token_ok=True,
            values=values,
            errors={},
            test_result=result,
            mail_saved=True,
        )
        return _render(request, "identity/setup.html", context)
    identity.complete_setup(db, actor)
    return _finish_setup()


def _finish_setup() -> Response:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SETUP_COOKIE, path="/setup")
    return response


# ------------------------------------------------------------------ sign-in


@router.get("/login", response_class=HTMLResponse)
@public
@screen("identity.login")
def login_page(request: Request, db: Db, next: str | None = None) -> Response:
    if (actor := current_actor(request)) and actor.mfa_satisfied:
        return RedirectResponse(safe_next(next), status_code=303)
    organisation = org.get_organization(db)
    return _render(
        request,
        "identity/login.html",
        {
            "next": safe_next(next),
            "email": "",
            "error": None,
            "organisation": organisation,
            "page_title": "Sign in",
        },
    )


@router.post("/login", response_class=HTMLResponse)
@public
@screen("identity.login")
def login_submit(
    request: Request, db: Db, email: FormStr = "", password: FormStr = "", next: FormStr = "/"
) -> Response:
    result = identity.authenticate(db, email, password, ip=client_ip(request))
    if result.status == "ok" and result.user_id is not None:
        return _signed_in_redirect(db, request, result.user_id, safe_next(next))
    if result.status == "locked":
        error = (
            f"Too many attempts. Your account is locked for {result.locked_minutes} minutes. "
            "An administrator can unlock it sooner."
        )
    elif result.status == "rate_limited":
        error = "Too many sign-in attempts from this network. Wait a few minutes and try again."
    elif result.attempts_left is not None:
        error = (
            f"That email and password don't match. {result.attempts_left} attempts left "
            "before your account is locked."
        )
    else:
        error = "That email and password don't match."
    return _render(
        request,
        "identity/login.html",
        {
            "next": safe_next(next),
            "email": email,
            "error": error,
            "organisation": org.get_organization(db),
            "page_title": "Sign in",
        },
        422,
    )


@router.get("/login/verify", response_class=HTMLResponse)
@pending_login
@screen("identity.mfa_verify")
def verify_page(
    request: Request, db: Db, actor: CurrentActor, next: str | None = None, recovery: bool = False
) -> Response:
    if actor.mfa_satisfied:
        return RedirectResponse(safe_next(next), status_code=303)
    if not actor.mfa_enabled:
        return RedirectResponse("/login/two-step", status_code=303)
    return _render(
        request,
        "identity/verify.html",
        {
            "next": safe_next(next),
            "error": None,
            "recovery": recovery,
            "organisation": org.get_organization(db),
            "page_title": "Two-step verification",
        },
    )


@router.post("/login/verify", response_class=HTMLResponse)
@pending_login
@screen("identity.mfa_verify")
def verify_submit(
    request: Request,
    db: Db,
    actor: CurrentActor,
    code: FormStr = "",
    next: FormStr = "/",
    recovery: Annotated[bool, Form()] = False,
) -> Response:
    ok = (
        identity.verify_recovery_code(db, actor, code)
        if recovery
        else identity.verify_mfa(db, actor, code)
    )
    if ok:
        return RedirectResponse(safe_next(next), status_code=303)
    error = (
        "That recovery code isn't valid or was already used."
        if recovery
        else "That code didn't work. Enter the newest 6-digit code from your app."
    )
    return _render(
        request,
        "identity/verify.html",
        {
            "next": safe_next(next),
            "error": error,
            "recovery": recovery,
            "organisation": org.get_organization(db),
            "page_title": "Two-step verification",
        },
        422,
    )


@router.get("/login/two-step", response_class=HTMLResponse)
@pending_login
@screen("identity.mfa_enrol")
def enrol_page(request: Request, db: Db, actor: CurrentActor) -> Response:
    if actor.mfa_enabled:
        return RedirectResponse("/", status_code=303)
    return _render(
        request,
        "identity/enrol.html",
        {
            "enrolment": identity.mfa_enrolment(db, actor),
            "error": None,
            "required": actor.mfa_required,
            "page_title": "Set up two-step verification",
        },
    )


@router.post("/login/two-step", response_class=HTMLResponse)
@pending_login
@screen("identity.mfa_enrol")
def enrol_submit(request: Request, db: Db, actor: CurrentActor, code: FormStr = "") -> Response:
    try:
        codes = identity.confirm_mfa_enrolment(db, actor, code)
    except ValidationFailed as exc:
        return _render(
            request,
            "identity/enrol.html",
            {
                "enrolment": identity.mfa_enrolment(db, actor),
                "error": exc.field_errors.get("code"),
                "required": actor.mfa_required,
                "page_title": "Set up two-step verification",
            },
            422,
        )
    return _render(
        request,
        "identity/recovery_codes.html",
        {"codes": codes, "continue_to": "/", "page_title": "Save your recovery codes"},
    )


@router.post("/logout")
@pending_login
def logout(request: Request, db: Db, actor: CurrentActor) -> Response:
    identity.logout(db, actor)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ------------------------------------------------------------------ password reset


@router.get("/forgot-password", response_class=HTMLResponse)
@public
@screen("identity.password_reset")
def forgot_page(request: Request, db: Db) -> HTMLResponse:
    return _render(
        request,
        "identity/forgot.html",
        {
            "sent": False,
            "organisation": org.get_organization(db),
            "page_title": "Reset your password",
        },
    )


@router.post("/forgot-password", response_class=HTMLResponse)
@public
@screen("identity.password_reset")
def forgot_submit(request: Request, db: Db, email: FormStr = "") -> HTMLResponse:
    identity.request_password_reset(db, email)
    return _render(
        request,
        "identity/forgot.html",
        {
            "sent": True,
            "organisation": org.get_organization(db),
            "page_title": "Reset your password",
        },
    )


@router.get("/reset-password/{token}", response_class=HTMLResponse)
@public
@screen("identity.password_reset")
def reset_page(request: Request, db: Db, token: str) -> HTMLResponse:
    view = identity.password_reset_view(db, token)
    if view is None:
        raise NotFound("This link has expired or has already been used. Ask for a new one.")
    return _render(
        request,
        "identity/reset.html",
        {
            "token": token,
            "view": view,
            "errors": {},
            "organisation": org.get_organization(db),
            "page_title": "Choose a new password",
        },
    )


@router.post("/reset-password/{token}", response_class=HTMLResponse)
@public
@screen("identity.password_reset")
def reset_submit(
    request: Request, db: Db, token: str, new_password: FormStr = "", confirm_password: FormStr = ""
) -> Response:
    try:
        user_id = identity.reset_password(db, token, new_password, confirm_password)
    except ValidationFailed as exc:
        view = identity.password_reset_view(db, token)
        return _render(
            request,
            "identity/reset.html",
            {
                "token": token,
                "view": view,
                "errors": exc.field_errors,
                "organisation": org.get_organization(db),
                "page_title": "Choose a new password",
            },
            422,
        )
    return _signed_in_redirect(db, request, user_id, "/")


# ------------------------------------------------------------------ invitations


@router.get("/invite/{token}", response_class=HTMLResponse)
@public
@screen("identity.invite_accept")
def invite_page(request: Request, db: Db, token: str) -> HTMLResponse:
    view = identity.invitation_view(db, token)
    if view is None:
        raise NotFound(EXPIRED_INVITE)
    return _render(
        request,
        "identity/invite.html",
        {
            "token": token,
            "view": view,
            "full_name": view.full_name,
            "errors": {},
            "organisation": org.get_organization(db),
            "page_title": "Create your account",
        },
    )


@router.post("/invite/{token}", response_class=HTMLResponse)
@public
@screen("identity.invite_accept")
def invite_submit(
    request: Request,
    db: Db,
    token: str,
    full_name: FormStr = "",
    password: FormStr = "",
    password_confirm: FormStr = "",
) -> Response:
    try:
        user_id = identity.accept_invitation(db, token, full_name, password, password_confirm)
    except ValidationFailed as exc:
        view = identity.invitation_view(db, token)
        return _render(
            request,
            "identity/invite.html",
            {
                "token": token,
                "view": view,
                "full_name": full_name,
                "errors": exc.field_errors,
                "organisation": org.get_organization(db),
                "page_title": "Create your account",
            },
            422,
        )
    return _signed_in_redirect(db, request, user_id, "/")


# ------------------------------------------------------------------ my security


def _security_context(db: Session, request: Request, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": build_shell(db, actor, active_module="home", crumbs=["My account", "Security"]),
        "overview": identity.security_overview(db, actor),
        "errors": {},
        "page_title": "Security",
        **extra,
    }


@router.get("/me/security", response_class=HTMLResponse)
@requires_login
@screen("identity.security")
def security_page(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "identity/security.html", _security_context(db, request, actor))


@router.post("/me/security/password", response_class=HTMLResponse)
@requires_login
@screen("identity.security")
def change_password(
    request: Request,
    db: Db,
    actor: CurrentActor,
    current_password: FormStr = "",
    new_password: FormStr = "",
    confirm_password: FormStr = "",
) -> HTMLResponse:
    try:
        identity.change_password(db, actor, current_password, new_password, confirm_password)
    except ValidationFailed as exc:
        return _render(
            request,
            "identity/security.html",
            _security_context(db, request, actor, errors=exc.field_errors, open_password=True),
            422,
        )
    return _render(
        request,
        "identity/security.html",
        _security_context(
            db, request, actor, notice="Password changed. You've been signed out everywhere else."
        ),
    )


@router.post("/me/sessions/{session_id}/revoke", response_class=HTMLResponse)
@requires_login
@screen("identity.security")
def revoke_session(
    request: Request, db: Db, actor: CurrentActor, session_id: uuid.UUID
) -> HTMLResponse:
    identity.revoke_my_session(db, actor, session_id)
    return _render(
        request,
        "identity/security.html",
        _security_context(db, request, actor, notice="That device has been signed out."),
    )


@router.post("/me/sessions/revoke-others", response_class=HTMLResponse)
@requires_login
@screen("identity.security")
def revoke_other_sessions(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    count = identity.revoke_other_sessions(db, actor)
    return _render(
        request,
        "identity/security.html",
        _security_context(
            db,
            request,
            actor,
            notice=f"Signed out of {count} other device{'s' if count != 1 else ''}.",
        ),
    )


@router.post("/me/security/recovery-codes", response_class=HTMLResponse)
@requires_login
@screen("identity.mfa_enrol")
def regenerate_codes(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    codes = identity.regenerate_recovery_codes(db, actor)
    return _render(
        request,
        "identity/recovery_codes.html",
        {"codes": codes, "continue_to": "/me/security", "page_title": "Your new recovery codes"},
    )


@router.post("/me/api-keys", response_class=HTMLResponse)
@requires_login
@screen("identity.security")
def create_api_key(
    request: Request,
    db: Db,
    actor: CurrentActor,
    name: FormStr = "",
    expires_in_days: Annotated[int | None, Form()] = 90,
) -> HTMLResponse:
    try:
        raw = identity.create_api_key(db, actor, name, expires_in_days)
    except ValidationFailed as exc:
        return _render(
            request,
            "identity/security.html",
            _security_context(db, request, actor, errors=exc.field_errors, open_api_key=True),
            422,
        )
    return _render(
        request, "identity/security.html", _security_context(db, request, actor, new_key=raw)
    )


@router.post("/me/api-keys/{key_id}/revoke", response_class=HTMLResponse)
@requires_login
@screen("identity.security")
def revoke_api_key(
    request: Request, db: Db, actor: CurrentActor, key_id: uuid.UUID
) -> HTMLResponse:
    identity.revoke_api_key(db, actor, key_id)
    return _render(
        request,
        "identity/security.html",
        _security_context(
            db, request, actor, notice="API key revoked. It stops working immediately."
        ),
    )


# ------------------------------------------------------------------ administration: users


def _users_context(
    db: Session, actor: Actor, status: str | None, q: str | None, **extra: Any
) -> dict[str, Any]:
    return {
        "shell": build_shell(
            db,
            actor,
            active_module="admin",
            active_section="Users",
            crumbs=["Administration", "Users"],
        ),
        "users": identity.list_users(db, status=status, query=q),
        "status": status or "all",
        "q": q or "",
        "mail_ready": mail.is_configured(db),
        "can_invite": bool(engine.check(db, actor.user_id, "identity.user.invite")),
        "can_manage_users": bool(engine.check(db, actor.user_id, "identity.user.manage")),
        "invite_errors": {},
        "invite_values": {},
        "page_title": "Users",
        **extra,
    }


@router.get("/admin/users", response_class=HTMLResponse)
@requires("identity.user.read")
@screen("admin.users")
def users_page(
    request: Request, db: Db, actor: CurrentActor, status: str | None = None, q: str | None = None
) -> HTMLResponse:
    return _render(request, "identity/users.html", _users_context(db, actor, status, q))


@router.post("/admin/users", response_class=HTMLResponse)
@requires("identity.user.invite")
@screen("admin.users")
def invite_user(
    request: Request, db: Db, actor: CurrentActor, full_name: FormStr = "", email: FormStr = ""
) -> HTMLResponse:
    values = {"full_name": full_name, "email": email}
    try:
        identity.invite_user(db, actor, InviteInput(full_name=full_name, email=email))
    except ValidationError as exc:
        return _render(
            request,
            "identity/users.html",
            _users_context(
                db,
                actor,
                None,
                None,
                invite_errors=field_errors(exc),
                invite_values=values,
                invite_open=True,
            ),
            422,
        )
    except (ValidationFailed, Conflict) as exc:
        errors = exc.field_errors if isinstance(exc, ValidationFailed) else {"form": exc.message}
        return _render(
            request,
            "identity/users.html",
            _users_context(
                db, actor, None, None, invite_errors=errors, invite_values=values, invite_open=True
            ),
            422,
        )
    return _render(
        request,
        "identity/users.html",
        _users_context(
            db, actor, None, None, notice=f"Invitation sent to {email.strip().lower()}."
        ),
    )


@router.post("/admin/users/{user_id}/{action}", response_class=HTMLResponse)
@requires("identity.user.manage")
@screen("admin.users")
def user_action(
    request: Request, db: Db, actor: CurrentActor, user_id: uuid.UUID, action: str
) -> HTMLResponse:
    handlers = {
        "resend": (identity.resend_invite, "Invitation sent again."),
        "suspend": (identity.suspend_user, "Account suspended and signed out everywhere."),
        "reactivate": (identity.reactivate_user, "Account reactivated."),
        "unlock": (identity.unlock_user, "Account unlocked."),
    }
    if action not in handlers:
        raise NotFound()
    func, message = handlers[action]
    try:
        func(db, actor, user_id)
    except Conflict as exc:
        return _render(
            request,
            "identity/users.html",
            _users_context(db, actor, None, None, problem=exc.message),
            409,
        )
    return _render(
        request, "identity/users.html", _users_context(db, actor, None, None, notice=message)
    )


# ------------------------------------------------------------------ administration: mail


def _mail_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": build_shell(
            db,
            actor,
            active_module="admin",
            active_section="Mail settings",
            crumbs=["Administration", "Mail settings"],
        ),
        "settings": mail.get_settings_view(db),
        "stats": mail.outbox_stats(db),
        "errors": {},
        "values": {},
        "page_title": "Mail settings",
        **extra,
    }


@router.get("/admin/mail", response_class=HTMLResponse)
@requires("system.mail.manage")
@screen("admin.mail")
def mail_page(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "identity/mail_settings.html", _mail_context(db, actor))


@router.post("/admin/mail", response_class=HTMLResponse)
@requires("system.mail.manage")
@screen("admin.mail")
def mail_save(
    request: Request,
    db: Db,
    actor: CurrentActor,
    action: FormStr = "save",
    host: FormStr = "",
    port: Annotated[int, Form()] = 587,
    security: FormStr = "starttls",
    username: OptionalFormStr = None,
    password: OptionalFormStr = None,
    from_name: FormStr = "",
    from_address: FormStr = "",
    reply_to: OptionalFormStr = None,
) -> HTMLResponse:
    values = {
        "host": host,
        "port": port,
        "security": security,
        "username": username,
        "from_name": from_name,
        "from_address": from_address,
        "reply_to": reply_to or None,
    }
    try:
        data = MailSettingsInput(**values, password=password)
    except ValidationError as exc:
        return _render(
            request,
            "identity/mail_settings.html",
            _mail_context(db, actor, errors=field_errors(exc), values=values),
            422,
        )
    mail.save_settings(db, data, actor_id=actor.user_id, actor_label=actor.label)
    result = None
    if action == "test":
        organisation = org.get_organization(db)
        result = mail.send_test(
            db,
            to=actor.email,
            name=actor.full_name.split()[0],
            organisation=organisation.display_name if organisation else "",
            actor_id=actor.user_id,
            actor_label=actor.label,
        )
    return _render(
        request,
        "identity/mail_settings.html",
        _mail_context(
            db, actor, test_result=result, notice=None if result else "Mail settings saved."
        ),
    )
