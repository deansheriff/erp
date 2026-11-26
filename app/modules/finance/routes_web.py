"""Finance web routes (ADR 0023): the chart of accounts, journals, recurring journals,
financial periods and the reports. Routes translate forms and render; the finance
services do the work."""

import csv
import io
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.declarations import requires, requires_login, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.identity.schemas import Actor
from app.modules.finance import accounts, journals, periods, project_link, recurring, reports
from app.modules.finance.accounts import CASH_FLOW_LABELS, TYPE_LABELS, AccountInput
from app.modules.finance.models import ACCOUNT_TYPES
from app.org import service as org
from app.web.access import require_actor
from app.web.forms import parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
FormList = Annotated[list[str], Form()]
DomainProblem = ValidationFailed | Conflict | PermissionDenied

STATUS_TONES = {"draft": "neutral", "held": "warning", "posted": "success"}
REPORTS = (
    ("tb", "Trial balance"),
    ("pl", "Profit and loss"),
    ("bs", "Balance sheet"),
    ("cf", "Cash flow"),
    ("gl", "General ledger"),
)
EMPTY_ROWS = 3
MIN_ROWS = 4


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str) -> Any:
    return build_shell(
        db, actor, active_module="finance", active_section=section, crumbs=["Finance", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _month(value: str) -> date | None:
    try:
        year, month = (int(p) for p in value.split("-"))
        return date(year, month, 1)
    except ValueError:
        return None


def _line_inputs(
    account_id: list[str],
    cost_centre_id: list[str],
    description: list[str],
    debit: list[str],
    credit: list[str],
    project_id: list[str] | None = None,
) -> list[journals.LineInput]:
    rows = max(len(account_id), len(debit), len(credit))
    projects = project_id or []

    def at(values: list[str], i: int) -> str:
        return values[i] if i < len(values) else ""

    return [
        journals.LineInput(
            at(account_id, i),
            at(cost_centre_id, i),
            at(description, i),
            at(debit, i),
            at(credit, i),
            at(projects, i),
        )
        for i in range(rows)
    ]


def _form_rows(lines: list[Any]) -> list[Any]:
    """Lines to show on a form: what there is, then a few empty rows."""
    blank = journals.LineInput("", "", "", "", "")
    count = max(len(lines) + EMPTY_ROWS, MIN_ROWS)
    return [*lines, *([blank] * (count - len(lines)))]


def _pickers(db: Session) -> dict[str, Any]:
    return {
        "postable": accounts.postable(db),
        "cost_centres": org.cost_centre_choices(db),
    }


# ------------------------------------------------------------------ chart of accounts


def _accounts_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor, "Chart of accounts"),
        "rows": accounts.tree(db, actor),
        "headers": [a for a in accounts.all_accounts(db) if a.is_header],
        "types": [(t, TYPE_LABELS[t]) for t in ACCOUNT_TYPES],
        "cash_flows": list(CASH_FLOW_LABELS.items()),
        "type_labels": TYPE_LABELS,
        "cash_flow_labels": CASH_FLOW_LABELS,
        "can_manage": bool(engine.check(db, actor.user_id, accounts.MANAGE)),
        "editing": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Chart of accounts",
        **extra,
    }


@router.get("/finance/accounts", response_class=HTMLResponse)
@requires("finance.account.read")
@screen("finance.accounts")
def chart(
    request: Request, db: Db, actor: CurrentActor, edit: str = "", notice: str = ""
) -> HTMLResponse:
    context = _accounts_context(db, actor, editing=edit, notice="Saved." if notice else None)
    return _render(request, "finance/accounts.html", context)


@router.post("/finance/accounts", response_class=HTMLResponse)
@requires("finance.account.manage")
@screen("finance.accounts")
def save_account(
    request: Request,
    db: Db,
    actor: CurrentActor,
    account_id: FormStr = "",
    code: FormStr = "",
    name: FormStr = "",
    type: FormStr = "",
    parent_id: FormStr = "",
    is_header: FormStr = "",
    cash_flow: FormStr = "",
    description: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    data = AccountInput(
        code=code,
        name=name,
        type=type,
        parent_id=_uuid(parent_id),
        is_header=bool(is_header),
        cash_flow=cash_flow,
        description=description,
        is_active=bool(is_active) or not account_id,
    )
    try:
        with db.begin_nested():
            saved = accounts.save_account(db, actor, _uuid(account_id), data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "code": code,
            "name": name,
            "type": type,
            "parent_id": parent_id,
            "is_header": is_header,
            "cash_flow": cash_flow,
            "description": description,
        }
        context = _accounts_context(
            db, actor, editing=account_id or "new", values=values, errors=_errors(exc)
        )
        return _render(request, "finance/accounts.html", context, 422)
    return _see_other(f"/finance/accounts?notice=1#a-{saved}")


# ------------------------------------------------------------------ journals


@router.get("/finance", response_class=HTMLResponse)
@router.get("/finance/journals", response_class=HTMLResponse)
@requires("finance.journal.read")
@screen("finance.journals")
def journal_list(
    request: Request,
    db: Db,
    actor: CurrentActor,
    tab: str = "draft",
    source: str = "",
    start: str = "",
    end: str = "",
    notice: str = "",
) -> HTMLResponse:
    recurring.generate_due(db)  # the nightly task also does this
    tab = tab if tab in journals.TABS else "draft"
    notices = {"deleted": "Draft deleted.", "posted": "Posted."}
    context = {
        "shell": _shell(db, actor, "Journals"),
        "tab": tab,
        "rows": journals.list_journals(
            db, actor, tab, source=source, start=parse_day(start), end=parse_day(end)
        ),
        "counts": journals.counts(db),
        "sources": list(journals.SOURCE_LABELS.items()),
        "filters": {"source": source, "start": start, "end": end},
        "tones": STATUS_TONES,
        "can_create": bool(engine.check(db, actor.user_id, journals.CREATE)),
        "notice": notices.get(notice),
        "page_title": "Journals",
    }
    return _render(request, "finance/journals.html", context)


def _journal_context(
    db: Session, actor: Actor, journal_id: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    view = journals.get_journal(db, actor, journal_id) if journal_id else None
    lines = extra.pop("lines", None)
    if lines is None:
        lines = (
            [
                journals.LineInput(
                    str(line.account_id or ""),
                    str(line.cost_centre_id or ""),
                    line.description or "",
                    f"{line.debit:.2f}" if line.debit else "",
                    f"{line.credit:.2f}" if line.credit else "",
                    str(line.project_id or ""),
                )
                for line in view.lines
            ]
            if view
            else []
        )
    return {
        "shell": _shell(db, actor, "Journals"),
        "journal": view,
        "rows": _form_rows(lines),
        "tones": STATUS_TONES,
        "today": clock.today(),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": (
            view.number or ("Held journal" if view.status == "held" else "Draft journal")
        )
        if view
        else "New journal",
        "projects": project_link.link().choices(db),
        **_pickers(db),
        **extra,
    }


@router.get("/finance/journals/new", response_class=HTMLResponse)
@requires("finance.journal.create")
@screen("finance.journal")
def new_journal(request: Request, db: Db, actor: CurrentActor) -> HTMLResponse:
    return _render(request, "finance/journal.html", _journal_context(db, actor, None))


@router.get("/finance/journals/{journal_id:uuid}", response_class=HTMLResponse)
@requires("finance.journal.read")
@screen("finance.journal")
def journal_page(
    request: Request, db: Db, actor: CurrentActor, journal_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    notices = {
        "saved": "Draft saved. Someone else posts it.",
        "posted": "Posted.",
        "reversed": "Reversed.",
    }
    context = _journal_context(db, actor, journal_id, notice=notices.get(notice))
    return _render(request, "finance/journal.html", context)


@router.post("/finance/journals", response_class=HTMLResponse)
@requires("finance.journal.create")
@screen("finance.journal")
def save_journal(
    request: Request,
    db: Db,
    actor: CurrentActor,
    journal_id: FormStr = "",
    journal_date: FormStr = "",
    memo: FormStr = "",
    account_id: FormList = [],  # noqa: B006  (FastAPI copies form defaults)
    cost_centre_id: FormList = [],  # noqa: B006
    description: FormList = [],  # noqa: B006
    debit: FormList = [],  # noqa: B006
    credit: FormList = [],  # noqa: B006
    project_id: FormList = [],  # noqa: B006
) -> Response:
    lines = _line_inputs(account_id, cost_centre_id, description, debit, credit, project_id)
    try:
        with db.begin_nested():
            saved = journals.save_draft(
                db,
                actor,
                _uuid(journal_id),
                journal_date=parse_day(journal_date),
                memo=memo,
                lines=lines,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        kept = [line for line in lines if line.account_id or line.debit or line.credit]
        context = _journal_context(
            db,
            actor,
            _uuid(journal_id),
            lines=kept,
            values={"journal_date": journal_date, "memo": memo},
            errors=_errors(exc),
        )
        return _render(request, "finance/journal.html", context, 422)
    return _see_other(f"/finance/journals/{saved}?notice=saved")


@router.post("/finance/journals/{journal_id:uuid}/{action}", response_class=HTMLResponse)
@requires_login
@screen("finance.journal")
def journal_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    journal_id: uuid.UUID,
    action: str,
    reverse_on: FormStr = "",
    new_date: FormStr = "",
    reason: FormStr = "",
) -> Response:
    if action not in {"post", "delete", "reverse", "redate"}:
        raise NotFound()
    try:
        with db.begin_nested():
            if action == "post":
                journals.post(db, actor, journal_id)
            elif action == "delete":
                journals.delete_draft(db, actor, journal_id)
            elif action == "redate":
                journals.redate_held(
                    db, actor, journal_id, new_date=parse_day(new_date), reason=reason
                )
            else:
                reversal = journals.reverse(db, actor, journal_id, on=parse_day(reverse_on))
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {"reverse_on": reverse_on, "new_date": new_date, "reason": reason}
        context = _journal_context(db, actor, journal_id, values=values, errors=_errors(exc))
        return _render(request, "finance/journal.html", context, 422)
    if action == "delete":
        return _see_other("/finance/journals?notice=deleted")
    if action == "reverse":
        return _see_other(f"/finance/journals/{reversal}?notice=reversed")
    return _see_other(f"/finance/journals/{journal_id}?notice=posted")


# ------------------------------------------------------------------ recurring journals


def _recurring_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    templates_ = recurring.list_templates(db, actor)
    editing = extra.get("editing", "")
    chosen = next((t for t in templates_ if str(t.id) == editing), None)
    lines = extra.pop("lines", None)
    if lines is None:
        lines = [
            journals.LineInput(
                str(line.account_id or ""),
                str(line.cost_centre_id or ""),
                line.description or "",
                f"{line.debit:.2f}" if line.debit else "",
                f"{line.credit:.2f}" if line.credit else "",
            )
            for line in (chosen.lines if chosen else [])
        ]
    return {
        "shell": _shell(db, actor, "Recurring"),
        "templates": templates_,
        "chosen": chosen,
        "rows": _form_rows(lines),
        "can_create": bool(engine.check(db, actor.user_id, journals.CREATE)),
        "this_month": clock.today().strftime("%Y-%m"),
        "editing": editing,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Recurring journals",
        **_pickers(db),
        **extra,
    }


@router.get("/finance/recurring", response_class=HTMLResponse)
@requires("finance.journal.read")
@screen("finance.recurring")
def recurring_page(
    request: Request, db: Db, actor: CurrentActor, edit: str = "", notice: str = ""
) -> HTMLResponse:
    context = _recurring_context(db, actor, editing=edit, notice="Saved." if notice else None)
    return _render(request, "finance/recurring.html", context)


@router.post("/finance/recurring", response_class=HTMLResponse)
@requires("finance.journal.create")
@screen("finance.recurring")
def save_recurring(
    request: Request,
    db: Db,
    actor: CurrentActor,
    template_id: FormStr = "",
    name: FormStr = "",
    memo: FormStr = "",
    day_of_month: FormStr = "",
    first_month: FormStr = "",
    last_month: FormStr = "",
    is_active: FormStr = "",
    account_id: FormList = [],  # noqa: B006
    cost_centre_id: FormList = [],  # noqa: B006
    description: FormList = [],  # noqa: B006
    debit: FormList = [],  # noqa: B006
    credit: FormList = [],  # noqa: B006
) -> Response:
    lines = _line_inputs(account_id, cost_centre_id, description, debit, credit)
    try:
        with db.begin_nested():
            recurring.save_template(
                db,
                actor,
                _uuid(template_id),
                name=name,
                memo=memo,
                day_of_month=day_of_month,
                first_month=_month(first_month),
                last_month=_month(last_month) if last_month else None,
                is_active=bool(is_active) or not template_id,
                lines=lines,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        kept = [line for line in lines if line.account_id or line.debit or line.credit]
        values = {
            "name": name,
            "memo": memo,
            "day_of_month": day_of_month,
            "first_month": first_month,
            "last_month": last_month,
        }
        context = _recurring_context(
            db,
            actor,
            editing=template_id or "new",
            lines=kept,
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "finance/recurring.html", context, 422)
    return _see_other("/finance/recurring?notice=1")


# ------------------------------------------------------------------ periods


def _periods_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    years = periods.years(db, actor)
    latest = max((y.year for y in years), default=clock.today().year - 1)
    return {
        "shell": _shell(db, actor, "Periods"),
        "years": years,
        "next_year": latest + 1 if years else clock.today().year,
        "can_manage": bool(engine.check(db, actor.user_id, periods.MANAGE)),
        "retained": periods.settings(db).retained_earnings_code,
        "equity_accounts": [a for a in accounts.postable(db) if a.type == "equity"],
        "failed": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Periods",
        **extra,
    }


@router.get("/finance/periods", response_class=HTMLResponse)
@requires("finance.journal.read")
@screen("finance.periods")
def periods_page(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    notices = {
        "open": "The year is open.",
        "close": "Closed.",
        "reopen": "Reopened.",
        "year": "The year is closed and its result is in retained earnings.",
        "settings": "Saved.",
    }
    context = _periods_context(db, actor, notice=notices.get(notice))
    return _render(request, "finance/periods.html", context)


@router.post("/finance/periods/years", response_class=HTMLResponse)
@requires("finance.period.manage")
@screen("finance.periods")
def open_year(request: Request, db: Db, actor: CurrentActor, year: FormStr = "") -> Response:
    try:
        with db.begin_nested():
            periods.open_year(db, actor, int(year) if year.isdigit() else 0)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _periods_context(db, actor, errors=_errors(exc), failed="year")
        return _render(request, "finance/periods.html", context, 422)
    return _see_other("/finance/periods?notice=open")


@router.post("/finance/periods/settings", response_class=HTMLResponse)
@requires("finance.period.manage")
@screen("finance.periods")
def save_period_settings(
    request: Request, db: Db, actor: CurrentActor, retained_earnings_code: FormStr = ""
) -> Response:
    try:
        with db.begin_nested():
            periods.save_settings(db, actor, retained_earnings_code=retained_earnings_code)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _periods_context(db, actor, errors=_errors(exc), failed="settings")
        return _render(request, "finance/periods.html", context, 422)
    return _see_other("/finance/periods?notice=settings")


@router.post("/finance/periods/{item_id:uuid}/{action}", response_class=HTMLResponse)
@requires("finance.period.manage")
@screen("finance.periods")
def period_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    item_id: uuid.UUID,
    action: str,
    reason: FormStr = "",
) -> Response:
    if action not in {"close", "reopen", "close_year"}:
        raise NotFound()
    try:
        with db.begin_nested():
            if action == "close":
                periods.close_period(db, actor, item_id)
            elif action == "reopen":
                periods.reopen_period(db, actor, item_id, reason=reason)
            else:
                journals.close_year(db, actor, item_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _periods_context(
            db, actor, errors=_errors(exc), failed=str(item_id), values={"reason": reason}
        )
        return _render(request, "finance/periods.html", context, 422)
    notice = {"close": "close", "reopen": "reopen", "close_year": "year"}[action]
    return _see_other(f"/finance/periods?notice={notice}")


# ------------------------------------------------------------------ reports


def _range(start: str, end: str) -> tuple[date, date]:
    today = clock.today()
    first = parse_day(start) or date(today.year, 1, 1)
    last = parse_day(end) or today
    return (first, last) if first <= last else (last, first)


@router.get("/finance/reports", response_class=HTMLResponse)
@requires("finance.report.read")
@screen("finance.reports")
def reports_page(
    request: Request,
    db: Db,
    actor: CurrentActor,
    report: str = "tb",
    start: str = "",
    end: str = "",
    as_at: str = "",
    cost_centre: str = "",
    account: str = "",
) -> HTMLResponse:
    report = report if report in dict(REPORTS) else "tb"
    first, last = _range(start, end)
    at = parse_day(as_at) or clock.today()
    postable = accounts.postable(db)
    chosen = _uuid(account) or (postable[0].id if postable else None)
    context: dict[str, Any] = {
        "shell": _shell(db, actor, "Reports"),
        "report": report,
        "reports": REPORTS,
        "start": first,
        "end": last,
        "as_at": at,
        "cost_centres": org.cost_centre_choices(db),
        "cost_centre": cost_centre,
        "postable": postable,
        "account": str(chosen or ""),
        "data": None,
        "page_title": dict(REPORTS)[report],
    }
    if report == "tb":
        context["data"] = reports.trial_balance(db, actor, at)
    elif report == "pl":
        context["data"] = reports.profit_and_loss(db, actor, first, last, _uuid(cost_centre))
    elif report == "bs":
        context["data"] = reports.balance_sheet(db, actor, at)
    elif report == "cf":
        context["data"] = reports.cash_flow(db, actor, first, last)
    elif chosen is not None:
        context["data"] = reports.general_ledger(db, actor, chosen, first, last)
    return _render(request, "finance/reports.html", context)


def _csv(filename: str, header: list[str], rows: list[list[object]]) -> Response:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(
            ["'" + v if isinstance(v, str) and v[:1] in {"=", "+", "-", "@"} else v for v in row]
        )
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/finance/reports/trial-balance.csv")
@requires("finance.report.read")
def trial_balance_csv(db: Db, actor: CurrentActor, as_at: str = "") -> Response:
    at = parse_day(as_at) or clock.today()
    tb = reports.trial_balance(db, actor, at)
    rows: list[list[object]] = [
        [r.code, r.name, f"{r.debit:.2f}", f"{r.credit:.2f}"] for r in tb.rows
    ]
    rows.append(["", "Total", f"{tb.debit:.2f}", f"{tb.credit:.2f}"])
    return _csv(f"trial-balance-{at.isoformat()}.csv", ["Code", "Account", "Debit", "Credit"], rows)


@router.get("/finance/reports/ledger.csv")
@requires("finance.report.read")
def ledger_csv(
    db: Db, actor: CurrentActor, account: str = "", start: str = "", end: str = ""
) -> Response:
    account_id = _uuid(account)
    if account_id is None:
        raise NotFound()
    first, last = _range(start, end)
    ledger = reports.general_ledger(db, actor, account_id, first, last)
    rows: list[list[object]] = [["", "", "Opening balance", "", "", "", f"{ledger.opening:.2f}"]]
    rows += [
        [
            line.journal_date.isoformat(),
            line.number,
            line.memo,
            line.description or "",
            f"{line.debit:.2f}",
            f"{line.credit:.2f}",
            f"{line.balance:.2f}",
        ]
        for line in ledger.lines
    ]
    return _csv(
        f"ledger-{ledger.account.code}-{first.isoformat()}-{last.isoformat()}.csv",
        ["Date", "Journal", "Memo", "Line", "Debit", "Credit", "Balance"],
        rows,
    )
