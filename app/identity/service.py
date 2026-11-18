"""Identity: installation, sign-in, sessions, MFA, invitations, password resets and API
keys (docs/IDENTITY.md). Routes call these functions; nothing here knows about HTTP."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.audit.service import AuditEvent, record, record_independent
from app.authz import service as authz
from app.core import clock
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.formatting import format_datetime
from app.core.secrets import DecryptionFailed, decrypt, encrypt, hash_token, new_token
from app.identity import mfa, passwords
from app.identity.models import (
    ApiKey,
    Installation,
    LoginAttempt,
    MfaRecoveryCode,
    SecuritySettings,
    User,
    UserSession,
    UserToken,
)
from app.identity.schemas import (
    Actor,
    AdminAccountInput,
    ApiKeyView,
    InviteInput,
    LoginResult,
    MfaEnrolment,
    SecurityOverview,
    SessionView,
    TokenView,
    UserList,
    UserRow,
)
from app.mail import service as mail
from app.org import service as org

SETUP_TOKEN_MINUTES = 60
BAD_TOTP = "That code didn't work. Check the time on your phone and try the newest code."
EXPIRED_INVITE = (
    "This invitation has expired or has already been used. Ask your administrator to resend it."
)
MAIL_NOT_READY = (
    "Set up mail first: invitations are sent by email. Go to Administration → Mail settings."
)
SESSION_TOUCH_SECONDS = 60
_installed_cache = False


# ------------------------------------------------------------------ helpers


def _settings(db: Session) -> SecuritySettings:
    return db.scalars(select(SecuritySettings)).one()


def _installation(db: Session) -> Installation:
    return db.scalars(select(Installation)).one()


def _audit(db: Session, action: str, actor: Actor | None = None, **kwargs: object) -> None:
    record(
        db,
        AuditEvent(
            action=action,
            actor_type="user" if actor else "anonymous",
            actor_id=actor.user_id if actor else None,
            actor_label=actor.label if actor else None,
            **kwargs,  # type: ignore[arg-type]
        ),
    )


def _actor_for(
    db: Session,
    user: User,
    *,
    via: str,
    session: UserSession | None = None,
    api_key: ApiKey | None = None,
) -> Actor:
    return Actor(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        mfa_enabled=user.mfa_enabled,
        mfa_required=authz.requires_mfa(db, user.id),
        mfa_satisfied=True if api_key else bool(session and session.mfa_satisfied),
        via="api_key" if api_key else "session",
        session_id=session.id if session else None,
        csrf_token=session.csrf_token if session else None,
        api_key_id=api_key.id if api_key else None,
    )


def _org_name(db: Session) -> str:
    view = org.get_organization(db)
    return view.display_name if view else "your organisation"


def _link(path: str) -> str:
    return get_settings().base_url.rstrip("/") + path


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def _password_problems(db: Session, password: str, *, name: str, email: str) -> list[str]:
    return passwords.password_problems(
        password,
        min_length=_settings(db).password_min_length,
        personal=(name, email, _org_name(db)),
    )


def _new_user_token(db: Session, user: User, purpose: str, ttl: timedelta) -> str:
    db.execute(
        update(UserToken)
        .where(
            UserToken.user_id == user.id, UserToken.purpose == purpose, UserToken.used_at.is_(None)
        )
        .values(used_at=clock.now())
    )
    raw = new_token()
    db.add(
        UserToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_token(raw),
            expires_at=clock.now() + ttl,
        )
    )
    return raw


def _valid_token(db: Session, raw: str, purpose: str) -> tuple[UserToken, User] | None:
    token = db.scalars(
        select(UserToken).where(
            UserToken.token_hash == hash_token(raw), UserToken.purpose == purpose
        )
    ).one_or_none()
    if token is None or token.used_at is not None or token.expires_at <= clock.now():
        return None
    user = db.get(User, token.user_id)
    return (token, user) if user else None


def _revoke_sessions(
    db: Session, user_id: uuid.UUID, reason: str, *, keep: uuid.UUID | None = None
) -> int:
    stmt = update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if keep is not None:
        stmt = stmt.where(UserSession.id != keep)
    result = db.execute(stmt.values(revoked_at=clock.now(), revoked_reason=reason))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _set_password(user: User, password: str) -> None:
    user.password_hash = passwords.hash_password(password)
    user.password_changed_at = clock.now()
    user.failed_login_count = 0
    user.locked_until = None


def _store_recovery_codes(db: Session, user: User) -> list[str]:
    db.execute(
        update(MfaRecoveryCode)
        .where(MfaRecoveryCode.user_id == user.id, MfaRecoveryCode.used_at.is_(None))
        .values(used_at=clock.now())
    )
    codes = mfa.new_recovery_codes()
    for code in codes:
        db.add(MfaRecoveryCode(user_id=user.id, code_hash=passwords.hash_password(code)))
    return codes


# ------------------------------------------------------------------ installation


def is_installed(db: Session) -> bool:
    global _installed_cache  # noqa: PLW0603  (installation is permanent once complete)
    if _installed_cache:
        return True
    _installed_cache = _installation(db).installed_at is not None
    return _installed_cache


def issue_setup_token(db: Session) -> str:
    """A new single-use setup code; any earlier code stops working."""
    install = _installation(db)
    if install.installed_at is not None:
        raise Conflict("Kxmeleon is already set up.")
    raw = new_token(9).replace("-", "X").replace("_", "Y").upper()[:12]
    code = f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}"
    install.setup_token_hash = hash_token(code)
    install.setup_token_expires_at = clock.now() + timedelta(minutes=SETUP_TOKEN_MINUTES)
    return code


def check_setup_token(db: Session, code: str | None) -> bool:
    install = _installation(db)
    if install.installed_at is not None or not code or install.setup_token_hash is None:
        return False
    if install.setup_token_expires_at is None or install.setup_token_expires_at <= clock.now():
        return False
    return hash_token(code.strip().upper()) == install.setup_token_hash


def setup_progress(db: Session) -> int:
    """1 = needs organisation, 2 = needs super admin, 3 = needs mail (optional)."""
    if org.get_organization(db) is None:
        return 1
    if db.scalar(select(func.count()).select_from(User)) == 0:
        return 2
    return 3


def setup_mfa_secret() -> str:
    return mfa.new_secret()


def setup_mfa_enrolment(secret: str, email: str, organisation: str) -> MfaEnrolment:
    uri = mfa.provisioning_uri(secret, email or "super admin", organisation)
    return MfaEnrolment(secret_grouped=mfa.grouped(secret), qr_svg=mfa.qr_svg(uri))


def setup_create_super_admin(
    db: Session, data: AdminAccountInput, mfa_secret: str
) -> tuple[uuid.UUID, list[str]]:
    if setup_progress(db) != 2:
        raise Conflict("The super admin has already been created.")
    email = _normalise_email(str(data.email))
    errors: dict[str, str] = {}
    problems = _password_problems(db, data.password, name=data.full_name, email=email)
    if problems:
        errors["password"] = " ".join(problems)
    elif data.password != data.password_confirm:
        errors["password_confirm"] = "The passwords don't match."
    step = mfa.verify_code(mfa_secret, data.mfa_code, None)
    if step is None:
        errors["mfa_code"] = BAD_TOTP
    if errors:
        raise ValidationFailed(field_errors=errors)
    now = clock.now()
    user = User(
        email=email,
        full_name=data.full_name.strip(),
        status="active",
        mfa_secret_encrypted=encrypt(mfa_secret),
        mfa_enabled_at=now,
        last_login_at=now,
        mfa_last_step=step,
    )
    _set_password(user, data.password)
    db.add(user)
    db.flush()
    codes = _store_recovery_codes(db, user)
    authz.grant_super_admin_at_setup(db, user.id)
    actor = _actor_for(db, user, via="session")
    _audit(
        db,
        "identity.user.create",
        actor,
        entity_type="user",
        entity_id=str(user.id),
        reason="super admin created during setup",
    )
    _audit(db, "identity.mfa.enrol", actor, entity_type="user", entity_id=str(user.id))
    return user.id, codes


def complete_setup(db: Session, actor: Actor) -> None:
    global _installed_cache  # noqa: PLW0603
    install = _installation(db)
    if install.installed_at is not None:
        raise Conflict("Kxmeleon is already set up.")
    install.installed_at = clock.now()
    install.setup_token_hash = None
    install.setup_token_expires_at = None
    _audit(db, "setup.complete", actor)
    _installed_cache = False  # re-read after commit


# ------------------------------------------------------------------ sign-in


def _rate_limited(db: Session, ip: str | None, settings: SecuritySettings) -> bool:
    if not ip:
        return False
    since = clock.now() - timedelta(minutes=settings.ip_attempt_window_minutes)
    attempts = (
        db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.ip == ip,
                LoginAttempt.attempted_at >= since,
                LoginAttempt.succeeded.is_(False),
            )
        )
        or 0
    )
    return attempts >= settings.ip_attempt_limit


def authenticate(db: Session, email: str, password: str, *, ip: str | None) -> LoginResult:
    """Check credentials. Failures are audited in their own transaction (ADR 0007)."""
    settings = _settings(db)
    email = _normalise_email(email)
    if _rate_limited(db, ip, settings):
        record_independent(
            AuditEvent(
                action="identity.login",
                actor_type="anonymous",
                outcome="denied",
                reason="rate_limited",
                actor_label=email,
            )
        )
        return LoginResult("rate_limited")

    user = db.scalars(select(User).where(User.email == email)).one_or_none()
    valid = passwords.verify_password(user.password_hash if user else None, password)
    if ip:
        db.add(LoginAttempt(ip=ip, succeeded=bool(valid and user)))

    now = clock.now()
    if user is not None and user.locked_until and user.locked_until > now:
        record_independent(
            AuditEvent(
                action="identity.login",
                actor_type="anonymous",
                outcome="denied",
                actor_label=email,
                reason="locked",
                entity_type="user",
                entity_id=str(user.id),
            )
        )
        minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        return LoginResult("locked", locked_minutes=minutes)

    if user is None or not valid or user.status != "active":
        attempts_left = None
        if user is not None and user.status == "active":
            user.failed_login_count += 1
            if user.failed_login_count >= settings.lockout_threshold:
                user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
                user.failed_login_count = 0
                attempts_left = 0
            else:
                attempts_left = settings.lockout_threshold - user.failed_login_count
        record_independent(
            AuditEvent(
                action="identity.login",
                actor_type="anonymous",
                outcome="denied",
                actor_label=email,
                reason="invalid_credentials",
            )
        )
        if attempts_left == 0:
            return LoginResult("locked", locked_minutes=settings.lockout_minutes)
        return LoginResult("invalid", attempts_left=attempts_left)

    if user.password_hash and passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    _audit(
        db,
        "identity.login",
        _actor_for(db, user, via="session"),
        entity_type="user",
        entity_id=str(user.id),
    )
    return LoginResult("ok", user_id=user.id)


def create_session(
    db: Session, user_id: uuid.UUID, *, ip: str | None, user_agent: str | None, mfa_satisfied: bool
) -> str:
    """Start a session and return the raw cookie value (only its hash is stored)."""
    raw = new_token(32)
    db.add(
        UserSession(
            user_id=user_id,
            token_hash=hash_token(raw),
            csrf_token=new_token(32),
            mfa_satisfied=mfa_satisfied,
            ip=ip,
            user_agent=(user_agent or "")[:400] or None,
            expires_at=clock.now() + timedelta(hours=_settings(db).session_absolute_hours),
        )
    )
    return raw


def session_needs_mfa(db: Session, user_id: uuid.UUID) -> bool:
    user = db.get(User, user_id)
    return bool(user and (user.mfa_enabled or authz.requires_mfa(db, user.id)))


def resolve_session(db: Session, raw: str | None) -> Actor | None:
    if not raw:
        return None
    session = db.scalars(
        select(UserSession).where(UserSession.token_hash == hash_token(raw))
    ).one_or_none()
    if session is None or session.revoked_at is not None:
        return None
    now = clock.now()
    idle = timedelta(minutes=_settings(db).session_idle_minutes)
    if session.expires_at <= now or session.last_seen_at + idle <= now:
        session.revoked_at = now
        session.revoked_reason = "expired"
        return None
    user = db.get(User, session.user_id)
    if user is None or user.status != "active":
        return None
    if (now - session.last_seen_at).total_seconds() >= SESSION_TOUCH_SECONDS:
        session.last_seen_at = now
    return _actor_for(db, user, via="session", session=session)


def logout(db: Session, actor: Actor) -> None:
    if actor.session_id:
        session = db.get(UserSession, actor.session_id)
        if session and session.revoked_at is None:
            session.revoked_at = clock.now()
            session.revoked_reason = "signed_out"
    _audit(db, "identity.logout", actor)


# ------------------------------------------------------------------ MFA


def _user(db: Session, actor: Actor) -> User:
    user = db.get(User, actor.user_id)
    if user is None:
        raise NotFound()
    return user


def verify_mfa(db: Session, actor: Actor, code: str) -> bool:
    user = _user(db, actor)
    if not user.mfa_secret_encrypted or not user.mfa_enabled:
        return False
    try:
        secret = decrypt(user.mfa_secret_encrypted)
    except DecryptionFailed:
        return False
    step = mfa.verify_code(secret, code, user.mfa_last_step)
    if step is None:
        record_independent(
            AuditEvent(
                action="identity.mfa.verify",
                actor_type="user",
                outcome="denied",
                actor_id=actor.user_id,
                actor_label=actor.label,
            )
        )
        return False
    user.mfa_last_step = step
    _mark_session_mfa(db, actor)
    return True


def verify_recovery_code(db: Session, actor: Actor, code: str) -> bool:
    normalised = mfa.normalise_recovery_code(code)
    for row in db.scalars(
        select(MfaRecoveryCode).where(
            MfaRecoveryCode.user_id == actor.user_id, MfaRecoveryCode.used_at.is_(None)
        )
    ):
        if passwords.verify_password(row.code_hash, normalised):
            row.used_at = clock.now()
            _mark_session_mfa(db, actor)
            _audit(db, "identity.recovery_code.use", actor)
            return True
    record_independent(
        AuditEvent(
            action="identity.recovery_code.use",
            actor_type="user",
            outcome="denied",
            actor_id=actor.user_id,
            actor_label=actor.label,
        )
    )
    return False


def _mark_session_mfa(db: Session, actor: Actor) -> None:
    if actor.session_id:
        session = db.get(UserSession, actor.session_id)
        if session:
            session.mfa_satisfied = True


def mfa_enrolment(db: Session, actor: Actor) -> MfaEnrolment:
    """Show (and if needed create) the pending secret for an account without MFA."""
    user = _user(db, actor)
    if user.mfa_enabled:
        raise Conflict("Two-step verification is already on.")
    secret: str | None = None
    if user.mfa_secret_encrypted:
        try:
            secret = decrypt(user.mfa_secret_encrypted)
        except DecryptionFailed:
            secret = None
    if secret is None:
        secret = mfa.new_secret()
        user.mfa_secret_encrypted = encrypt(secret)
    uri = mfa.provisioning_uri(secret, user.email, _org_name(db))
    return MfaEnrolment(secret_grouped=mfa.grouped(secret), qr_svg=mfa.qr_svg(uri))


def confirm_mfa_enrolment(db: Session, actor: Actor, code: str) -> list[str]:
    user = _user(db, actor)
    if user.mfa_enabled or not user.mfa_secret_encrypted:
        raise Conflict("Start two-step verification set-up again.")
    step = mfa.verify_code(decrypt(user.mfa_secret_encrypted), code, None)
    if step is None:
        raise ValidationFailed(field_errors={"code": BAD_TOTP})
    user.mfa_enabled_at = clock.now()
    user.mfa_last_step = step
    codes = _store_recovery_codes(db, user)
    _mark_session_mfa(db, actor)
    _audit(db, "identity.mfa.enrol", actor, entity_type="user", entity_id=str(user.id))
    return codes


def regenerate_recovery_codes(db: Session, actor: Actor) -> list[str]:
    user = _user(db, actor)
    if not user.mfa_enabled:
        raise Conflict("Turn on two-step verification first.")
    codes = _store_recovery_codes(db, user)
    _audit(
        db, "identity.recovery_code.regenerate", actor, entity_type="user", entity_id=str(user.id)
    )
    return codes


# ------------------------------------------------------------------ my security


def _describe_agent(agent: str | None) -> tuple[str, str]:
    ua = (agent or "").lower()
    browser = next(
        (
            name
            for key, name in (
                ("edg/", "Edge"),
                ("chrome/", "Chrome"),
                ("firefox/", "Firefox"),
                ("safari/", "Safari"),
            )
            if key in ua
        ),
        "Browser",
    )
    system = next(
        (
            name
            for key, name in (
                ("iphone", "iPhone"),
                ("android", "Android"),
                ("windows", "Windows"),
                ("mac os", "Mac"),
                ("linux", "Linux"),
            )
            if key in ua
        ),
        "",
    )
    device = "mobile" if any(k in ua for k in ("iphone", "android", "mobile")) else "desktop"
    return (f"{browser} on {system}" if system else browser), device


def security_overview(db: Session, actor: Actor) -> SecurityOverview:
    user = _user(db, actor)
    now = clock.now()
    sessions = []
    for s in db.scalars(
        select(UserSession)
        .where(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.last_seen_at.desc())
    ):
        browser, device = _describe_agent(s.user_agent)
        sessions.append(
            SessionView(
                id=s.id,
                browser=browser,
                device=device,  # type: ignore[arg-type]
                ip=str(s.ip) if s.ip else None,
                last_seen_at=s.last_seen_at,
                is_current=s.id == actor.session_id,
            )
        )
    keys = [
        ApiKeyView(
            id=k.id,
            name=k.name,
            display=f"kx_{k.prefix}_••••",
            created_at=k.created_at,
            expires_at=k.expires_at,
            last_used_at=k.last_used_at,
        )
        for k in db.scalars(
            select(ApiKey)
            .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
    ]
    codes_left = (
        db.scalar(
            select(func.count())
            .select_from(MfaRecoveryCode)
            .where(MfaRecoveryCode.user_id == user.id, MfaRecoveryCode.used_at.is_(None))
        )
        or 0
    )
    return SecurityOverview(
        password_changed_at=user.password_changed_at,
        mfa_enabled_at=user.mfa_enabled_at,
        recovery_codes_left=codes_left,
        sessions=sessions,
        api_keys=keys,
    )


def revoke_my_session(db: Session, actor: Actor, session_id: uuid.UUID) -> None:
    session = db.get(UserSession, session_id)
    if session is None or session.user_id != actor.user_id:
        raise NotFound()
    if session.revoked_at is None:
        session.revoked_at = clock.now()
        session.revoked_reason = "revoked_by_user"
        _audit(
            db, "identity.session.revoke", actor, entity_type="session", entity_id=str(session_id)
        )


def revoke_other_sessions(db: Session, actor: Actor) -> int:
    count = _revoke_sessions(db, actor.user_id, "revoked_by_user", keep=actor.session_id)
    _audit(db, "identity.session.revoke", actor, reason=f"signed out of {count} other sessions")
    return count


def change_password(db: Session, actor: Actor, current: str, new: str, confirm: str) -> None:
    user = _user(db, actor)
    if not passwords.verify_password(user.password_hash, current):
        raise ValidationFailed(
            field_errors={"current_password": "That isn't your current password."}
        )
    problems = _password_problems(db, new, name=user.full_name, email=user.email)
    if problems:
        raise ValidationFailed(field_errors={"new_password": " ".join(problems)})
    if new != confirm:
        raise ValidationFailed(field_errors={"confirm_password": "The passwords don't match."})
    _set_password(user, new)
    _revoke_sessions(db, user.id, "password_changed", keep=actor.session_id)
    _audit(db, "identity.password.change", actor, entity_type="user", entity_id=str(user.id))


# ------------------------------------------------------------------ password reset


def request_password_reset(db: Session, email: str) -> None:
    """Always behaves the same, whether or not the address has an account."""
    user = db.scalars(select(User).where(User.email == _normalise_email(email))).one_or_none()
    if user is None or user.status != "active" or not mail.is_configured(db):
        return
    minutes = _settings(db).reset_ttl_minutes
    raw = _new_user_token(db, user, "password_reset", timedelta(minutes=minutes))
    mail.queue(
        db,
        to=user.email,
        template="password_reset",
        context={
            "name": user.full_name.split()[0],
            "organisation": _org_name(db),
            "link": _link(f"/reset-password/{raw}"),
            "minutes": minutes,
        },
    )
    record(
        db,
        AuditEvent(
            action="identity.password.reset_requested",
            actor_type="anonymous",
            entity_type="user",
            entity_id=str(user.id),
        ),
    )


def password_reset_view(db: Session, raw: str) -> TokenView | None:
    found = _valid_token(db, raw, "password_reset")
    if not found:
        return None
    token, user = found
    return TokenView(full_name=user.full_name, email=user.email, expires_at=token.expires_at)


def reset_password(db: Session, raw: str, new: str, confirm: str) -> uuid.UUID:
    found = _valid_token(db, raw, "password_reset")
    if not found:
        raise NotFound("This link has expired or has already been used. Ask for a new one.")
    token, user = found
    problems = _password_problems(db, new, name=user.full_name, email=user.email)
    if problems:
        raise ValidationFailed(field_errors={"new_password": " ".join(problems)})
    if new != confirm:
        raise ValidationFailed(field_errors={"confirm_password": "The passwords don't match."})
    token.used_at = clock.now()
    _set_password(user, new)
    _revoke_sessions(db, user.id, "password_reset")
    _audit(
        db,
        "identity.password.reset",
        _actor_for(db, user, via="session"),
        entity_type="user",
        entity_id=str(user.id),
    )
    return user.id


# ------------------------------------------------------------------ invitations and users


def _require_mail(db: Session) -> None:
    if not mail.is_configured(db):
        raise Conflict(MAIL_NOT_READY)


def invite_user(db: Session, actor: Actor, data: InviteInput) -> uuid.UUID:
    _require_mail(db)
    email = _normalise_email(str(data.email))
    if db.scalars(select(User).where(User.email == email)).one_or_none():
        raise ValidationFailed(
            field_errors={"email": "Someone with this email already has an account."}
        )
    user = User(
        email=email, full_name=data.full_name.strip(), status="invited", invited_by=actor.user_id
    )
    db.add(user)
    db.flush()
    _send_invite(db, actor, user)
    _audit(db, "identity.user.invite", actor, entity_type="user", entity_id=str(user.id))
    return user.id


def _send_invite(db: Session, actor: Actor, user: User) -> None:
    hours = _settings(db).invite_ttl_hours
    expires = clock.now() + timedelta(hours=hours)
    raw = _new_user_token(db, user, "invite", timedelta(hours=hours))
    org_view = org.get_organization(db)
    mail.queue(
        db,
        to=user.email,
        template="invite",
        context={
            "name": user.full_name.split()[0],
            "inviter": actor.full_name,
            "organisation": _org_name(db),
            "link": _link(f"/invite/{raw}"),
            "expires": format_datetime(
                expires, org_view.timezone if org_view else get_settings().timezone
            ),
        },
    )


def resend_invite(db: Session, actor: Actor, user_id: uuid.UUID) -> None:
    _require_mail(db)
    user = db.get(User, user_id)
    if user is None:
        raise NotFound()
    if user.status != "invited":
        raise Conflict("This person has already accepted their invitation.")
    _send_invite(db, actor, user)
    _audit(db, "identity.user.invite_resend", actor, entity_type="user", entity_id=str(user.id))


def invitation_view(db: Session, raw: str) -> TokenView | None:
    found = _valid_token(db, raw, "invite")
    if not found or found[1].status != "invited":
        return None
    token, user = found
    inviter = db.get(User, user.invited_by) if user.invited_by else None
    return TokenView(
        full_name=user.full_name,
        email=user.email,
        expires_at=token.expires_at,
        inviter_name=inviter.full_name if inviter else None,
    )


def accept_invitation(
    db: Session, raw: str, full_name: str, password: str, confirm: str
) -> uuid.UUID:
    found = _valid_token(db, raw, "invite")
    if not found or found[1].status != "invited":
        raise NotFound(EXPIRED_INVITE)
    token, user = found
    name = full_name.strip() or user.full_name
    errors: dict[str, str] = {}
    if len(name) < 2:
        errors["full_name"] = "Enter your full name."
    problems = _password_problems(db, password, name=name, email=user.email)
    if problems:
        errors["password"] = " ".join(problems)
    elif password != confirm:
        errors["password_confirm"] = "The passwords don't match."
    if errors:
        raise ValidationFailed(field_errors=errors)
    token.used_at = clock.now()
    user.full_name = name
    user.status = "active"
    _set_password(user, password)
    _audit(
        db,
        "identity.user.activate",
        _actor_for(db, user, via="session"),
        entity_type="user",
        entity_id=str(user.id),
    )
    return user.id


def list_users(db: Session, *, status: str | None = None, query: str | None = None) -> UserList:
    counts = dict(db.execute(select(User.status, func.count()).group_by(User.status)).all())
    counts["all"] = sum(counts.values())
    stmt = select(User).order_by(User.full_name)
    if status in {"active", "invited", "suspended", "deactivated"}:
        stmt = stmt.where(User.status == status)
    if query:
        like = f"%{query.strip().lower()}%"
        stmt = stmt.where(func.lower(User.full_name).like(like) | User.email.like(like))
    now = clock.now()
    users = list(db.scalars(stmt.limit(500)))
    roles = authz.role_names(db, [u.id for u in users])
    rows = [
        UserRow(
            id=u.id,
            full_name=u.full_name,
            email=u.email,
            status=u.status,
            mfa_enabled=u.mfa_enabled,
            roles=roles.get(u.id, []),
            last_login_at=u.last_login_at,
            locked=bool(u.locked_until and u.locked_until > now),
        )
        for u in users
    ]
    return UserList(rows=rows, counts=counts)


def suspend_user(db: Session, actor: Actor, user_id: uuid.UUID) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound()
    if user.id == actor.user_id:
        raise Conflict("You can't suspend your own account.")
    authz.ensure_not_last_super_admin(db, user.id)
    user.status = "suspended"
    authz.revoke_delegations_for(db, actor, user.id, "account suspended")
    _revoke_sessions(db, user.id, "user_suspended")
    db.execute(
        update(ApiKey)
        .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=clock.now())
    )
    _audit(db, "identity.user.suspend", actor, entity_type="user", entity_id=str(user.id))


def reactivate_user(db: Session, actor: Actor, user_id: uuid.UUID) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound()
    if user.status != "suspended":
        raise Conflict("Only suspended accounts can be reactivated.")
    user.status = "active"
    _audit(db, "identity.user.reactivate", actor, entity_type="user", entity_id=str(user.id))


def unlock_user(db: Session, actor: Actor, user_id: uuid.UUID) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound()
    user.locked_until = None
    user.failed_login_count = 0
    _audit(db, "identity.user.unlock", actor, entity_type="user", entity_id=str(user.id))


# ------------------------------------------------------------------ API keys


def create_api_key(db: Session, actor: Actor, name: str, expires_in_days: int | None) -> str:
    name = name.strip()
    if not 2 <= len(name) <= 100:
        raise ValidationFailed(
            field_errors={"name": "Give the key a name between 2 and 100 characters."}
        )
    if expires_in_days is not None and not 1 <= expires_in_days <= 730:
        raise ValidationFailed(
            field_errors={"expires_in_days": "Choose an expiry between 1 day and 2 years."}
        )
    prefix = new_token(6)[:8].replace("-", "x").replace("_", "y")
    raw = f"kx_{prefix}_{new_token(32)}"
    key = ApiKey(
        user_id=actor.user_id,
        name=name,
        prefix=prefix,
        key_hash=hash_token(raw),
        expires_at=clock.now() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    db.add(key)
    db.flush()
    _audit(db, "identity.api_key.create", actor, entity_type="api_key", entity_id=str(key.id))
    return raw


def revoke_api_key(db: Session, actor: Actor, key_id: uuid.UUID) -> None:
    key = db.get(ApiKey, key_id)
    if key is None or key.user_id != actor.user_id:
        raise NotFound()
    if key.revoked_at is None:
        key.revoked_at = clock.now()
        _audit(db, "identity.api_key.revoke", actor, entity_type="api_key", entity_id=str(key.id))


def resolve_api_key(db: Session, raw: str | None) -> Actor | None:
    if not raw or not raw.startswith("kx_"):
        return None
    key = db.scalars(select(ApiKey).where(ApiKey.key_hash == hash_token(raw))).one_or_none()
    now = clock.now()
    if key is None or key.revoked_at is not None or (key.expires_at and key.expires_at <= now):
        return None
    user = db.get(User, key.user_id)
    if user is None or user.status != "active":
        return None
    if (
        key.last_used_at is None
        or (now - key.last_used_at).total_seconds() >= SESSION_TOUCH_SECONDS
    ):
        key.last_used_at = now
    return _actor_for(db, user, via="api_key", api_key=key)
