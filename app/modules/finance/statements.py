"""Bank statement import and matching (ADR 0026). CSV and Excel statements are read with
the account's layout; OFX directly. Lines already imported are skipped by fingerprint.
On import, a line with exactly one ledger candidate (same amount, within the match days)
is matched; rules turn others into entries. People match, unmatch and give entries by
hand until the reconciliation that covers a line is approved."""

import csv
import hashlib
import io
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.files import service as files
from app.identity.schemas import Actor
from app.modules.finance import accounts, banking, journals, periods
from app.modules.finance.banking import READ, RECONCILE, require, uuid_or_none
from app.modules.finance.models import (
    Account,
    BankAccount,
    BankStatement,
    Journal,
    JournalLine,
    StatementLine,
)
from app.org import service as org

ZERO = Decimal(0)
CENT = Decimal("0.01")
MAX_LINES = 5000
OFX_TRANSACTION = re.compile(
    r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", re.S | re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ParsedLine:
    line_date: date
    description: str
    reference: str | None
    amount: Decimal


# ------------------------------------------------------------------ parsing


def _amount(text: object) -> Decimal | None:
    if text is None:
        return None
    if isinstance(text, (int, float, Decimal)):
        return Decimal(str(text)).quantize(CENT)
    value = str(text).strip().replace(",", "").replace("₦", "").replace("NGN", "").strip()
    if not value:
        return None
    negative = value.startswith("(") and value.endswith(")")
    value = value.strip("()")
    for suffix, sign in (("CR", 1), ("DR", -1)):
        if value.upper().endswith(suffix):
            value, negative = value[: -len(suffix)].strip(), sign < 0
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return (-amount if negative else amount).quantize(CENT)


def _date(value: object, fmt: str) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for candidate in (fmt, "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, candidate).date()  # noqa: DTZ007  (a calendar date)
        except ValueError:
            continue
    return None


def _column(header: list[str], spec: str | None) -> int | None:
    """A column by header (any case) or 1-based number; None when not given."""
    if not spec:
        return None
    if spec.isdigit():
        return int(spec) - 1
    names = [h.strip().lower() for h in header]
    return names.index(spec.strip().lower()) if spec.strip().lower() in names else -1


def _rows(bank: BankAccount, rows: list[list[Any]]) -> tuple[list[ParsedLine], list[str]]:
    rows = rows[bank.csv_skip_rows :]
    if not rows:
        return [], ["The file has no rows after the ones to skip."]
    header = [str(c or "") for c in rows[0]]
    cols = {
        "date": _column(header, bank.csv_date),
        "description": _column(header, bank.csv_description),
        "reference": _column(header, bank.csv_reference),
        "amount": _column(header, bank.csv_amount),
        "in": _column(header, bank.csv_money_in),
        "out": _column(header, bank.csv_money_out),
    }
    missing = [k for k, v in cols.items() if v == -1]
    if missing:
        return [], [
            "These columns aren't in the file's header: "
            + ", ".join(missing)
            + ". Check the account's statement layout."
        ]
    lines, problems = [], []
    for number, row in enumerate(rows[1:], start=bank.csv_skip_rows + 2):

        def cell(key: str, row: list[Any] = row) -> Any:
            i = cols[key]
            return row[i] if i is not None and i < len(row) else None

        if not any(str(c or "").strip() for c in row):
            continue
        day = _date(cell("date"), bank.csv_date_format)
        if cols["amount"] is not None:
            amount = _amount(cell("amount"))
        else:
            money_in, money_out = _amount(cell("in")) or ZERO, _amount(cell("out")) or ZERO
            amount = abs(money_in) - abs(money_out)
        if day is None or amount is None:
            problems.append(f"Row {number}: couldn't read the date or amount.")
            continue
        if amount == 0:
            continue
        reference = str(cell("reference") or "").strip() or None
        lines.append(
            ParsedLine(day, str(cell("description") or "").strip()[:300] or "—", reference, amount)
        )
    return lines, problems


def _ofx(text: str) -> tuple[list[ParsedLine], list[str]]:
    def tag(block: str, name: str) -> str:
        found = re.search(rf"<{name}>([^<\r\n]*)", block, re.I)
        return found.group(1).strip() if found else ""

    lines, problems = [], []
    for i, block in enumerate(OFX_TRANSACTION.findall(text), start=1):
        posted = tag(block, "DTPOSTED")[:8]
        amount = _amount(tag(block, "TRNAMT"))
        try:
            day = datetime.strptime(posted, "%Y%m%d").date()  # noqa: DTZ007  (a calendar date)
        except ValueError:
            day = None
        if day is None or amount is None:
            problems.append(f"Transaction {i}: couldn't read the date or amount.")
            continue
        description = " ".join(p for p in (tag(block, "NAME"), tag(block, "MEMO")) if p)
        lines.append(ParsedLine(day, description[:300] or "—", tag(block, "FITID") or None, amount))
    if not lines and not problems:
        problems.append("No transactions were found in the OFX file.")
    return lines, problems


def parse(bank: BankAccount, filename: str, data: bytes) -> tuple[list[ParsedLine], list[str]]:
    """Statement lines and the problems found, from CSV, Excel or OFX bytes."""
    if data[:2] == b"PK":
        try:
            sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        except Exception:  # noqa: BLE001  (any unreadable workbook)
            return [], ["The Excel file couldn't be read."]
        if sheet is None:
            return [], ["The Excel file has no sheet."]
        return _rows(bank, [list(r) for r in sheet.iter_rows(values_only=True)])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if "<OFX>" in text.upper() or filename.lower().endswith(".ofx"):
        return _ofx(text)
    return _rows(bank, [list(r) for r in csv.reader(io.StringIO(text))])


def fingerprints(lines: list[ParsedLine]) -> list[str]:
    """One per line: its details and which repeat of those details it is in the file."""
    seen: Counter[tuple[date, Decimal, str, str | None]] = Counter()
    out = []
    for line in lines:
        key = (line.line_date, line.amount, line.description, line.reference)
        seen[key] += 1
        raw = f"{line.line_date}|{line.amount}|{line.description}|{line.reference}|{seen[key]}"
        out.append(hashlib.sha256(raw.encode()).hexdigest())
    return out


# ------------------------------------------------------------------ importing and matching


@dataclass(frozen=True, slots=True)
class ImportResult:
    statement_id: uuid.UUID
    added: int
    skipped: int
    matched: int
    ruled: int
    problems: list[str] = field(default_factory=list)


def import_statement(
    db: Session, actor: Actor, bank_id: uuid.UUID, *, filename: str, data: bytes, mime: str | None
) -> ImportResult:
    require(db, actor, RECONCILE)
    bank = db.get(BankAccount, bank_id)
    if bank is None:
        raise NotFound()
    if not bank.is_active:
        raise Conflict("This bank account is inactive.")
    parsed, problems = parse(bank, filename, data)
    if not parsed:
        raise ValidationFailed(
            field_errors={"file": problems[0] if problems else "No lines found."}
        )
    if len(parsed) > MAX_LINES:
        raise ValidationFailed(
            field_errors={"file": f"A statement can have {MAX_LINES} lines at most."}
        )
    file_id = files.upload(
        db,
        actor,
        purpose_key="bank_statement",
        entity_id=str(bank.id),
        filename=filename,
        declared_mime=mime,
        data=data,
    )
    prints = fingerprints(parsed)
    existing = set(
        db.scalars(
            select(StatementLine.fingerprint).where(
                StatementLine.bank_account_id == bank.id, StatementLine.fingerprint.in_(prints)
            )
        )
    )
    statement = BankStatement(
        bank_account_id=bank.id,
        file_id=file_id,
        filename=filename[:255],
        first_date=min(p.line_date for p in parsed),
        last_date=max(p.line_date for p in parsed),
        imported_by=actor.user_id,
    )
    db.add(statement)
    db.flush()
    added = []
    for line, fingerprint in zip(parsed, prints, strict=True):
        if fingerprint in existing:
            continue
        row = StatementLine(
            bank_account_id=bank.id,
            statement_id=statement.id,
            line_date=line.line_date,
            description=line.description,
            reference=(line.reference or "")[:120] or None,
            amount=line.amount,
            fingerprint=fingerprint,
        )
        db.add(row)
        added.append(row)
    statement.line_count, statement.skipped = len(added), len(parsed) - len(added)
    db.flush()
    matched, ruled = auto_match(db, bank, added)
    record_by(
        db,
        actor,
        "finance.bank_statement.import",
        entity_type="bank_account",
        entity_id=str(bank.id),
        changes={"lines": len(added), "skipped": statement.skipped, "matched": matched},
    )
    return ImportResult(statement.id, len(added), statement.skipped, matched, ruled, problems)


def _taken(db: Session, journal_line_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not journal_line_ids:
        return set()
    return {
        jl
        for jl in db.scalars(
            select(StatementLine.journal_line_id).where(
                StatementLine.journal_line_id.in_(journal_line_ids)
            )
        )
        if jl
    }


@dataclass(frozen=True, slots=True)
class Candidate:
    journal_line_id: uuid.UUID
    journal_id: uuid.UUID
    number: str
    journal_date: date
    memo: str
    amount: Decimal


def candidates(
    db: Session, bank: BankAccount, line: StatementLine, *, days: int | None
) -> list[Candidate]:
    """Posted, unmatched ledger lines on the bank's ledger account with the same amount,
    within `days` of the line's date (any date when None)."""
    query = (
        select(JournalLine, Journal)
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(
            Journal.status == "posted",
            JournalLine.account_id == bank.account_id,
            (JournalLine.debit - JournalLine.credit) == line.amount,
        )
        .order_by(Journal.journal_date, Journal.number)
    )
    if days is not None:
        query = query.where(
            Journal.journal_date >= line.line_date - timedelta(days=days),
            Journal.journal_date <= line.line_date + timedelta(days=days),
        )
    rows = db.execute(query.limit(50)).all()
    taken = _taken(db, [jl.id for jl, _ in rows])
    return [
        Candidate(jl.id, j.id, j.number or "", j.journal_date, j.memo, jl.debit - jl.credit)
        for jl, j in rows
        if jl.id not in taken
    ]


def auto_match(db: Session, bank: BankAccount, lines: list[StatementLine]) -> tuple[int, int]:
    """Match lines with exactly one candidate in the window; rules for the rest. Returns
    (matched, given entries)."""
    days = periods.settings(db).match_days
    rule_list = banking.rules(db)
    claimed: set[uuid.UUID] = set()
    matched = ruled = 0
    for line in lines:
        if line.status != "unmatched" or line.reconciliation_id:
            continue
        found = [
            c for c in candidates(db, bank, line, days=days) if c.journal_line_id not in claimed
        ]
        if len(found) == 1:
            line.status, line.journal_line_id = "matched", found[0].journal_line_id
            claimed.add(found[0].journal_line_id)
            matched += 1
            continue
        rule = banking.rule_for(rule_list, bank.id, line.description, line.amount)
        if rule is not None:
            line.status, line.rule_id = "entry", rule.id
            line.entry_account_id, line.entry_cost_centre_id = rule.account_id, rule.cost_centre_id
            line.entry_description = rule.description or line.description[:200]
            ruled += 1
    db.flush()
    return matched, ruled


def rematch(db: Session, actor: Actor, bank_id: uuid.UUID) -> tuple[int, int]:
    """Try the open lines again, for ledger entries posted since the import."""
    require(db, actor, RECONCILE)
    bank = db.get(BankAccount, bank_id)
    if bank is None:
        raise NotFound()
    lines = list(
        db.scalars(
            select(StatementLine)
            .where(StatementLine.bank_account_id == bank.id, StatementLine.status == "unmatched")
            .order_by(StatementLine.line_date)
        )
    )
    return auto_match(db, bank, lines)


def _open_line(db: Session, line_id: uuid.UUID) -> StatementLine:
    line = db.scalars(
        select(StatementLine).where(StatementLine.id == line_id).with_for_update()
    ).first()
    if line is None:
        raise NotFound()
    if line.reconciliation_id:
        raise Conflict("This line is in an approved reconciliation, so it's locked.")
    return line


def match(db: Session, actor: Actor, line_id: uuid.UUID, journal_line_id: uuid.UUID) -> None:
    require(db, actor, RECONCILE)
    line = _open_line(db, line_id)
    if line.status != "unmatched":
        raise Conflict("Unmatch or clear this line first.")
    bank = db.get(BankAccount, line.bank_account_id)
    assert bank is not None  # noqa: S101  (foreign key)
    if journal_line_id not in {c.journal_line_id for c in candidates(db, bank, line, days=None)}:
        raise Conflict("That ledger entry isn't for this amount on this account, or is taken.")
    line.status, line.journal_line_id = "matched", journal_line_id
    db.flush()
    record_by(
        db,
        actor,
        "finance.bank_line.match",
        entity_type="bank_statement_line",
        entity_id=str(line.id),
    )


def unmatch(db: Session, actor: Actor, line_id: uuid.UUID) -> None:
    """Undo a match or an entry that hasn't posted."""
    require(db, actor, RECONCILE)
    line = _open_line(db, line_id)
    if line.entry_journal_id:
        raise Conflict("This entry has posted.")
    line.status, line.journal_line_id, line.rule_id = "unmatched", None, None
    line.entry_account_id = line.entry_cost_centre_id = None
    line.entry_description = None
    db.flush()
    record_by(
        db,
        actor,
        "finance.bank_line.unmatch",
        entity_type="bank_statement_line",
        entity_id=str(line.id),
    )


def give_entry(
    db: Session,
    actor: Actor,
    line_id: uuid.UUID,
    *,
    account_id: str,
    cost_centre_id: str,
    description: str,
) -> None:
    """An entry for a line with no ledger match (bank charges, interest …). It posts
    when the reconciliation is approved."""
    require(db, actor, RECONCILE)
    line = _open_line(db, line_id)
    if line.status != "unmatched":
        raise Conflict("Unmatch or clear this line first.")
    bank = db.get(BankAccount, line.bank_account_id)
    account = {a.id: a for a in accounts.postable(db)}.get(uuid_or_none(account_id))  # type: ignore[arg-type]
    errors = {}
    if account is None or (bank and account.id == bank.account_id):
        errors[f"entry_{line.id}"] = "Choose the account it goes to."
    centre = uuid_or_none(cost_centre_id)
    if centre and centre not in {cid for cid, _ in org.cost_centre_choices(db)}:
        errors[f"entry_{line.id}"] = "Choose an active cost centre."
    if errors:
        raise ValidationFailed(field_errors=errors)
    assert account is not None  # noqa: S101  (checked above)
    line.status, line.entry_account_id, line.entry_cost_centre_id = "entry", account.id, centre
    line.entry_description = description.strip()[:200] or line.description[:200]
    db.flush()
    record_by(
        db,
        actor,
        "finance.bank_line.entry",
        entity_type="bank_statement_line",
        entity_id=str(line.id),
        changes={"account": account.code},
    )


def post_entry(db: Session, line: StatementLine, poster: uuid.UUID | None) -> None:
    """Post a line's entry: the bank against the entry's account, on the line's date."""
    bank = db.get(BankAccount, line.bank_account_id)
    ledger = db.get(Account, bank.account_id) if bank else None
    contra = db.get(Account, line.entry_account_id) if line.entry_account_id else None
    assert ledger is not None and contra is not None  # noqa: S101  (foreign keys)
    amount = abs(line.amount)
    money_in = line.amount > 0
    journal = journals.post_document_journal(
        db,
        source_id=line.id,
        journal_date=line.line_date,
        memo=f"Bank: {line.entry_description or line.description}"[:200],
        lines=[
            (ledger.code, None, amount if money_in else ZERO, ZERO if money_in else amount),
            (
                contra.code,
                line.entry_cost_centre_id,
                ZERO if money_in else amount,
                amount if money_in else ZERO,
            ),
        ],
        poster=poster,
        source="bank",
    )
    line.entry_journal_id = journal.id
    line.journal_line_id = db.scalars(
        select(JournalLine.id).where(
            JournalLine.journal_id == journal.id, JournalLine.account_code == ledger.code
        )
    ).first()
    db.flush()


# ------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class LineView:
    id: uuid.UUID
    line_date: date
    description: str
    reference: str | None
    amount: Decimal
    status: str
    locked: bool
    journal: tuple[uuid.UUID, str, str] | None  # journal id, number, memo
    entry_account: str | None
    entry_description: str | None
    rule: str | None
    suggestions: list[Candidate] = field(default_factory=list)


def lines_for(
    db: Session, actor: Actor, bank_id: uuid.UUID, status: str, *, limit: int = 300
) -> list[LineView]:
    require(db, actor, READ)
    bank = db.get(BankAccount, bank_id)
    if bank is None:
        raise NotFound()
    rows = list(
        db.scalars(
            select(StatementLine)
            .where(StatementLine.bank_account_id == bank.id, StatementLine.status == status)
            .order_by(StatementLine.line_date.desc())
            .limit(limit)
        )
    )
    codes = {a.id: f"{a.code} {a.name}" for a in accounts.all_accounts(db)}
    journal_rows = {
        jl.id: (j.id, j.number or "", j.memo)
        for jl, j in db.execute(
            select(JournalLine, Journal)
            .join(Journal, Journal.id == JournalLine.journal_id)
            .where(JournalLine.id.in_([r.journal_line_id for r in rows if r.journal_line_id]))
        ).all()
    }
    rule_text = {r.id: f"“{r.contains}” → {codes.get(r.account_id, '')}" for r in banking.rules(db)}
    days = periods.settings(db).match_days
    out = []
    for r in rows:
        out.append(
            LineView(
                r.id,
                r.line_date,
                r.description,
                r.reference,
                r.amount,
                r.status,
                bool(r.reconciliation_id),
                journal_rows.get(r.journal_line_id) if r.journal_line_id else None,
                codes.get(r.entry_account_id) if r.entry_account_id else None,
                r.entry_description,
                rule_text.get(r.rule_id) if r.rule_id else None,
                candidates(db, bank, r, days=days * 10)[:3] if status == "unmatched" else [],
            )
        )
    return out


def counts(db: Session, bank_id: uuid.UUID) -> dict[str, int]:
    found = dict(
        db.execute(
            select(StatementLine.status, func.count())
            .where(StatementLine.bank_account_id == bank_id)
            .group_by(StatementLine.status)
        ).all()
    )
    return {s: found.get(s, 0) for s in ("unmatched", "matched", "entry")}


def statements_for(
    db: Session, actor: Actor, bank_id: uuid.UUID
) -> list[tuple[BankStatement, str | None]]:
    require(db, actor, READ)
    rows = list(
        db.scalars(
            select(BankStatement)
            .where(BankStatement.bank_account_id == bank_id)
            .order_by(BankStatement.imported_at.desc())
            .limit(20)
        )
    )
    names = journals.user_names(db, {r.imported_by for r in rows})
    return [(r, names.get(r.imported_by) if r.imported_by else None) for r in rows]
