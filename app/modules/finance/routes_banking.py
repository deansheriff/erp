"""Banking web routes (ADR 0026): bank accounts and rules, statement import and matching,
reconciliations and transfers. Routes translate forms and render; the services do the
work."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.authz.declarations import requires, screen
from app.core import clock
from app.core.db import get_db
from app.core.errors import Conflict, PermissionDenied, ValidationFailed
from app.files import service as files
from app.files.web import ReceivedUpload, upload_body
from app.identity.schemas import Actor
from app.modules.finance import (
    accounts,
    banking,
    periods,
    reconciliations,
    statements,
    transfers,
)
from app.modules.finance.banking import (
    APPROVE,
    MANAGE,
    RECONCILE,
    BankAccountInput,
    RuleInput,
    can,
    uuid_or_none,
)
from app.modules.finance.files import BANK_STATEMENT
from app.modules.finance.models import RULE_DIRECTIONS, BankAccount, BankRule
from app.org import service as org
from app.web.access import require_actor
from app.web.forms import parse_day
from app.web.shell import build_shell
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

Db = Annotated[Session, Depends(get_db)]
CurrentActor = Annotated[Actor, Depends(require_actor)]
FormStr = Annotated[str, Form()]
StatementUpload = Annotated[ReceivedUpload, Depends(upload_body(BANK_STATEMENT.max_bytes))]
DomainProblem = ValidationFailed | Conflict | PermissionDenied
TABS = (("unmatched", "Unmatched"), ("matched", "Matched"), ("entry", "Entries"))
LAYOUT_FIELDS = (
    "csv_date",
    "csv_description",
    "csv_reference",
    "csv_amount",
    "csv_money_in",
    "csv_money_out",
    "csv_date_format",
    "csv_skip_rows",
)


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status)


def _errors(exc: DomainProblem) -> dict[str, str]:
    return getattr(exc, "field_errors", None) or {"form": exc.message}


def _shell(db: Session, actor: Actor, section: str) -> Any:
    return build_shell(
        db, actor, active_module="finance", active_section=section, crumbs=["Finance", section]
    )


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ accounts and rules


def _accounts_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    codes = {a.id: f"{a.code} {a.name}" for a in accounts.all_accounts(db)}
    return {
        "shell": _shell(db, actor, "Banking"),
        "rows": banking.list_accounts(db, actor),
        "rules": banking.rules(db),
        "codes": codes,
        "bank_labels": dict(banking.choices(db)),
        "postable": accounts.postable(db),
        "cost_centres": org.cost_centre_choices(db),
        "directions": [(d, banking.DIRECTION_LABELS[d]) for d in RULE_DIRECTIONS],
        "direction_labels": banking.DIRECTION_LABELS,
        "can_manage": can(db, actor, MANAGE),
        "can_reconcile": can(db, actor, RECONCILE),
        "editing": "",
        "bank": None,
        "ledger_name": "",
        "editing_rule": None,
        "form": "",
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Banking",
        **extra,
    }


@router.get("/finance/banking", response_class=HTMLResponse)
@requires("finance.bank.read")
@screen("finance.bank_accounts")
def bank_accounts(
    request: Request,
    db: Db,
    actor: CurrentActor,
    edit: str = "",
    rule: str = "",
    notice: str = "",
) -> HTMLResponse:
    bank = db.get(BankAccount, uuid_or_none(edit)) if uuid_or_none(edit) else None
    rule_row = db.get(BankRule, uuid_or_none(rule)) if uuid_or_none(rule) else None
    context = _accounts_context(
        db,
        actor,
        editing="new" if edit == "new" else (str(bank.id) if bank else ""),
        bank=bank,
        ledger_name=banking.ledger_of(db, bank.id).name if bank else "",
        editing_rule=rule_row,
        notice="Saved." if notice else None,
    )
    return _render(request, "finance/bank_accounts.html", context)


@router.post("/finance/banking/accounts", response_class=HTMLResponse)
@requires("finance.bank.manage")
@screen("finance.bank_accounts")
def save_bank_account(
    request: Request,
    db: Db,
    actor: CurrentActor,
    bank_id: FormStr = "",
    name: FormStr = "",
    bank_name: FormStr = "",
    bank_code: FormStr = "",
    account_number: FormStr = "",
    account_name: FormStr = "",
    csv_date: FormStr = "Date",
    csv_description: FormStr = "Description",
    csv_reference: FormStr = "",
    csv_amount: FormStr = "",
    csv_money_in: FormStr = "",
    csv_money_out: FormStr = "",
    csv_date_format: FormStr = "%d/%m/%Y",
    csv_skip_rows: FormStr = "0",
    is_active: FormStr = "",
) -> Response:
    data = BankAccountInput(
        name,
        bank_name,
        bank_code,
        account_number,
        account_name,
        csv_date,
        csv_description,
        csv_reference,
        csv_amount,
        csv_money_in,
        csv_money_out,
        csv_date_format,
        csv_skip_rows,
        bool(is_active) or not bank_id,
    )
    existing = uuid_or_none(bank_id)
    try:
        with db.begin_nested():
            saved = banking.save_account(db, actor, existing, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "name": name,
            "bank_name": bank_name,
            "bank_code": bank_code,
            "account_number": account_number,
            "account_name": account_name,
            "csv_date": csv_date,
            "csv_description": csv_description,
            "csv_reference": csv_reference,
            "csv_amount": csv_amount,
            "csv_money_in": csv_money_in,
            "csv_money_out": csv_money_out,
            "csv_date_format": csv_date_format,
            "csv_skip_rows": csv_skip_rows,
        }
        context = _accounts_context(
            db,
            actor,
            editing=bank_id or "new",
            bank=db.get(BankAccount, existing) if existing else None,
            ledger_name=banking.ledger_of(db, existing).name if existing else "",
            form="account",
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "finance/bank_accounts.html", context, 422)
    return _see_other(f"/finance/banking/{saved}?notice=saved")


@router.post("/finance/banking/rules", response_class=HTMLResponse)
@requires("finance.bank.manage")
@screen("finance.bank_accounts")
def save_rule(
    request: Request,
    db: Db,
    actor: CurrentActor,
    rule_id: FormStr = "",
    bank_account_id: FormStr = "",
    contains: FormStr = "",
    direction: FormStr = "any",
    account_id: FormStr = "",
    cost_centre_id: FormStr = "",
    description: FormStr = "",
    is_active: FormStr = "",
) -> Response:
    data = RuleInput(
        bank_account_id,
        contains,
        direction,
        account_id,
        cost_centre_id,
        description,
        bool(is_active) or not rule_id,
    )
    existing = uuid_or_none(rule_id)
    try:
        with db.begin_nested():
            banking.save_rule(db, actor, existing, data)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "bank_account_id": bank_account_id,
            "contains": contains,
            "direction": direction,
            "account_id": account_id,
            "cost_centre_id": cost_centre_id,
            "description": description,
        }
        context = _accounts_context(
            db,
            actor,
            editing_rule=db.get(BankRule, existing) if existing else None,
            form="rule",
            values=values,
            errors=_errors(exc),
        )
        return _render(request, "finance/bank_accounts.html", context, 422)
    return _see_other("/finance/banking?notice=1#rules")


# ------------------------------------------------------------------ one account: import and match

NOTICES = {
    "saved": "Saved.",
    "rematched": "Matched again.",
    "matched": "Matched.",
    "unmatched": "Unmatched.",
    "entry": "Entry added. It posts when the reconciliation is approved.",
}


def _bank_context(
    db: Session, actor: Actor, bank_id: uuid.UUID, tab: str, **extra: Any
) -> dict[str, Any]:
    bank = banking.get_account(db, actor, bank_id)
    ledger = banking.ledger_of(db, bank.id)
    open_rec = reconciliations.open_for(db, bank.id)
    return {
        "shell": _shell(db, actor, "Banking"),
        "bank": bank,
        "ledger": ledger,
        "balance": banking.ledger_balance(db, ledger.id),
        "tab": tab,
        "tabs": TABS,
        "counts": statements.counts(db, bank.id),
        "lines": statements.lines_for(db, actor, bank.id, tab),
        "statements": statements.statements_for(db, actor, bank.id),
        "recs": reconciliations.for_account(db, bank.id),
        "open_rec": open_rec,
        "rec_labels": reconciliations.STATUS_LABELS,
        "rec_tones": reconciliations.STATUS_TONES,
        "postable": [a for a in accounts.postable(db) if a.id != ledger.id],
        "cost_centres": org.cost_centre_choices(db),
        "match_days": periods.settings(db).match_days,
        "can_reconcile": can(db, actor, RECONCILE),
        "can_manage": can(db, actor, MANAGE),
        "today": clock.today(),
        "result": None,
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": ledger.name,
        **extra,
    }


@router.get("/finance/banking/{bank_id:uuid}", response_class=HTMLResponse)
@requires("finance.bank.read")
@screen("finance.bank_account")
def bank_account(
    request: Request,
    db: Db,
    actor: CurrentActor,
    bank_id: uuid.UUID,
    tab: str = "unmatched",
    notice: str = "",
) -> HTMLResponse:
    tab = tab if tab in dict(TABS) else "unmatched"
    context = _bank_context(db, actor, bank_id, tab, notice=NOTICES.get(notice))
    return _render(request, "finance/bank_account.html", context)


@router.post("/finance/banking/{bank_id:uuid}/import", response_class=HTMLResponse)
@requires("finance.bank.reconcile")
@screen("finance.bank_account")
def import_statement(
    request: Request, db: Db, actor: CurrentActor, bank_id: uuid.UUID, upload: StatementUpload
) -> Response:
    try:
        if upload.errors:
            raise ValidationFailed(field_errors={"file": next(iter(upload.errors.values()))})
        with db.begin_nested():
            result = statements.import_statement(
                db,
                actor,
                bank_id,
                filename=upload.filename or "statement",
                data=upload.data,
                mime=upload.content_type,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _bank_context(db, actor, bank_id, "unmatched", errors=_errors(exc))
        return _render(request, "finance/bank_account.html", context, 422)
    files.request_scan()
    context = _bank_context(db, actor, bank_id, "unmatched", result=result)
    return _render(request, "finance/bank_account.html", context)


@router.post("/finance/banking/{bank_id:uuid}/rematch", response_class=HTMLResponse)
@requires("finance.bank.reconcile")
@screen("finance.bank_account")
def rematch(request: Request, db: Db, actor: CurrentActor, bank_id: uuid.UUID) -> Response:
    with db.begin_nested():
        statements.rematch(db, actor, bank_id)
    return _see_other(f"/finance/banking/{bank_id}?notice=rematched")


@router.post("/finance/banking/lines/{line_id:uuid}/{action}", response_class=HTMLResponse)
@requires("finance.bank.reconcile")
@screen("finance.bank_account")
def line_action(
    request: Request,
    db: Db,
    actor: CurrentActor,
    line_id: uuid.UUID,
    action: str,
    bank_id: FormStr = "",
    journal_line_id: FormStr = "",
    account_id: FormStr = "",
    cost_centre_id: FormStr = "",
    description: FormStr = "",
    tab: FormStr = "unmatched",
) -> Response:
    bank = uuid_or_none(bank_id)
    if bank is None:
        return _see_other("/finance/banking")
    try:
        with db.begin_nested():
            if action == "match":
                target = uuid_or_none(journal_line_id)
                if target is None:
                    raise Conflict("Choose the ledger entry it matches.")
                statements.match(db, actor, line_id, target)
            elif action == "unmatch":
                statements.unmatch(db, actor, line_id)
            elif action == "entry":
                statements.give_entry(
                    db,
                    actor,
                    line_id,
                    account_id=account_id,
                    cost_centre_id=cost_centre_id,
                    description=description,
                )
            else:
                raise Conflict("Choose what to do with the line.")
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _bank_context(db, actor, bank, tab, errors=_errors(exc))
        return _render(request, "finance/bank_account.html", context, 422)
    notice = {"match": "matched", "unmatch": "unmatched", "entry": "entry"}[action]
    return _see_other(f"/finance/banking/{bank}?tab={tab}&notice={notice}")


@router.post("/finance/banking/{bank_id:uuid}/reconcile", response_class=HTMLResponse)
@requires("finance.bank.reconcile")
@screen("finance.bank_account")
def start_reconciliation(
    request: Request,
    db: Db,
    actor: CurrentActor,
    bank_id: uuid.UUID,
    statement_date: FormStr = "",
    statement_balance: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            rec = reconciliations.start(
                db,
                actor,
                bank_id,
                statement_date=parse_day(statement_date),
                balance=statement_balance,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _bank_context(
            db,
            actor,
            bank_id,
            "unmatched",
            values={"statement_date": statement_date, "statement_balance": statement_balance},
            errors=_errors(exc),
        )
        return _render(request, "finance/bank_account.html", context, 422)
    return _see_other(f"/finance/reconciliations/{rec}")


# ------------------------------------------------------------------ reconciliations

REC_NOTICES = {
    "submitted": "Submitted. Someone other than you approves it.",
    "approved": "Approved. The entries are posted and the lines locked.",
    "returned": "Sent back to draft.",
}


@router.get("/finance/reconciliations/{rec_id:uuid}", response_class=HTMLResponse)
@requires("finance.bank.read")
@screen("finance.reconciliation")
def reconciliation(
    request: Request, db: Db, actor: CurrentActor, rec_id: uuid.UUID, notice: str = ""
) -> HTMLResponse:
    view = reconciliations.get(db, actor, rec_id)
    context = {
        "shell": _shell(db, actor, "Banking"),
        "rec": view,
        "f": view.figures,
        "labels": reconciliations.STATUS_LABELS,
        "tones": reconciliations.STATUS_TONES,
        "errors": {},
        "notice": REC_NOTICES.get(notice),
        "page_title": f"Reconciliation {view.statement_date:%d %b %Y}",
    }
    return _render(request, "finance/reconciliation.html", context)


@router.post("/finance/reconciliations/{rec_id:uuid}/{action}", response_class=HTMLResponse)
@requires("finance.bank.read")
@screen("finance.reconciliation")
def reconciliation_action(
    request: Request, db: Db, actor: CurrentActor, rec_id: uuid.UUID, action: str
) -> Response:
    """submit, approve or send-back: each service checks its own permission."""
    runs = {
        "submit": (reconciliations.submit, "submitted"),
        "approve": (reconciliations.approve, "approved"),
        "send-back": (reconciliations.send_back, "returned"),
    }
    if action not in runs:
        return _see_other(f"/finance/reconciliations/{rec_id}")
    run, notice = runs[action]
    try:
        with db.begin_nested():
            run(db, actor, rec_id)
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        view = reconciliations.get(db, actor, rec_id)
        context = {
            "shell": _shell(db, actor, "Banking"),
            "rec": view,
            "f": view.figures,
            "labels": reconciliations.STATUS_LABELS,
            "tones": reconciliations.STATUS_TONES,
            "errors": _errors(exc),
            "notice": None,
            "page_title": f"Reconciliation {view.statement_date:%d %b %Y}",
        }
        return _render(request, "finance/reconciliation.html", context, 422)
    return _see_other(f"/finance/reconciliations/{rec_id}?notice={notice}")


# ------------------------------------------------------------------ transfers


def _transfers_context(db: Session, actor: Actor, **extra: Any) -> dict[str, Any]:
    return {
        "shell": _shell(db, actor, "Transfers"),
        "rows": transfers.list_transfers(db, actor),
        "banks": banking.choices(db),
        "labels": transfers.STATUS_LABELS,
        "tones": transfers.STATUS_TONES,
        "can_record": can(db, actor, RECONCILE),
        "can_approve": can(db, actor, APPROVE),
        "today": clock.today(),
        "values": {},
        "errors": {},
        "notice": None,
        "page_title": "Transfers",
        **extra,
    }


@router.get("/finance/transfers", response_class=HTMLResponse)
@requires("finance.bank.read")
@screen("finance.transfers")
def transfer_list(request: Request, db: Db, actor: CurrentActor, notice: str = "") -> HTMLResponse:
    notices = {
        "recorded": "Recorded. Someone other than you approves it.",
        "approved": "Approved and posted.",
        "deleted": "Deleted.",
    }
    context = _transfers_context(db, actor, notice=notices.get(notice))
    return _render(request, "finance/transfers.html", context)


@router.post("/finance/transfers", response_class=HTMLResponse)
@requires("finance.bank.reconcile")
@screen("finance.transfers")
def record_transfer(
    request: Request,
    db: Db,
    actor: CurrentActor,
    from_id: FormStr = "",
    to_id: FormStr = "",
    amount: FormStr = "",
    on: FormStr = "",
    reference: FormStr = "",
) -> Response:
    try:
        with db.begin_nested():
            transfers.record(
                db,
                actor,
                from_id=from_id,
                to_id=to_id,
                amount=amount,
                on=parse_day(on),
                reference=reference,
            )
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        values = {
            "from_id": from_id,
            "to_id": to_id,
            "amount": amount,
            "on": on,
            "reference": reference,
        }
        context = _transfers_context(db, actor, values=values, errors=_errors(exc))
        return _render(request, "finance/transfers.html", context, 422)
    return _see_other("/finance/transfers?notice=recorded")


@router.post("/finance/transfers/{transfer_id:uuid}/{action}", response_class=HTMLResponse)
@requires("finance.bank.read")
@screen("finance.transfers")
def transfer_action(
    request: Request, db: Db, actor: CurrentActor, transfer_id: uuid.UUID, action: str
) -> Response:
    """approve or delete: each service checks its own permission."""
    try:
        with db.begin_nested():
            if action == "approve":
                transfers.approve(db, actor, transfer_id)
            elif action == "delete":
                transfers.delete(db, actor, transfer_id)
            else:
                raise Conflict("Choose what to do with the transfer.")
    except (ValidationFailed, Conflict, PermissionDenied) as exc:
        context = _transfers_context(db, actor, errors=_errors(exc))
        return _render(request, "finance/transfers.html", context, 422)
    return _see_other(
        f"/finance/transfers?notice={'approved' if action == 'approve' else 'deleted'}"
    )
