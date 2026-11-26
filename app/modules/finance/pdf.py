"""Sales document and statement PDFs (ADR 0024), drawn on demand from issued data with
reportlab's built-in fonts. They have no naira sign, so amounts read "NGN 1,250.00"."""

import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

INK = (0.086, 0.086, 0.165)
MUTED = (0.384, 0.384, 0.502)
LINE = (0.894, 0.894, 0.933)
WIDTH, HEIGHT = A4
LEFT, RIGHT = 20 * mm, WIDTH - 20 * mm
BOTTOM = 25 * mm


def money(amount: Decimal) -> str:
    rounded = amount.quantize(Decimal("0.01"))
    text = f"NGN {abs(rounded):,.2f}"
    return f"({text})" if rounded < 0 else text


def day(value: date | None) -> str:
    return f"{value:%d %b %Y}" if value else ""


@dataclass(frozen=True, slots=True)
class Party:
    name: str
    address: str | None
    tin: str | None
    extra: str | None = None  # RC number, or the customer's code


@dataclass(frozen=True, slots=True)
class DocumentPdf:
    title: str  # "Invoice", "Credit note"
    number: str
    seller: Party
    buyer: Party
    facts: list[tuple[str, str]]  # (label, value): date, due, reference
    lines: list[tuple[str, str, str, str, str]]  # description, qty, price, VAT, amount
    totals: list[tuple[str, str]]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class StatementPdf:
    seller: Party
    buyer: Party
    period: str
    opening: Decimal
    rows: list[tuple[str, str, str, str, str, str]]  # date, number, text, debit, credit, bal
    closing: Decimal
    aging: list[tuple[str, str]]


class _Page:
    """A canvas with a running y position that starts a new page when it runs out."""

    def __init__(self, pdf: Canvas, header: Callable[["_Page"], None]) -> None:
        self.pdf, self.header, self.y = pdf, header, HEIGHT - 22 * mm
        header(self)

    def text(
        self,
        x: float,
        value: str,
        *,
        size: float = 9,
        bold: bool = False,
        colour: tuple[float, float, float] = INK,
        align: str = "left",
    ) -> None:
        self.pdf.setFillColorRGB(*colour)
        self.pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        if align == "right":
            self.pdf.drawRightString(x, self.y, value)
        else:
            self.pdf.drawString(x, self.y, value[:95])

    def rule(self) -> None:
        self.pdf.setStrokeColorRGB(*LINE)
        self.pdf.line(LEFT, self.y + 3 * mm, RIGHT, self.y + 3 * mm)

    def down(self, amount: float) -> None:
        self.y -= amount
        if self.y < BOTTOM:
            self.pdf.showPage()
            self.y = HEIGHT - 22 * mm
            self.header(self)


def _parties(page: _Page, title: str, number: str, seller: Party, buyer: Party) -> None:
    page.text(LEFT, seller.name, size=14, bold=True)
    page.text(RIGHT, title.upper(), size=14, bold=True, align="right")
    page.down(5 * mm)
    page.text(RIGHT, number, size=10, colour=MUTED, align="right")
    for value in (seller.address, f"TIN {seller.tin}" if seller.tin else None, seller.extra):
        if value:
            for part in value.splitlines():
                page.text(LEFT, part, colour=MUTED)
                page.down(4.2 * mm)
    page.down(6 * mm)
    page.text(LEFT, "To", size=8, colour=MUTED)
    page.down(4.5 * mm)
    page.text(LEFT, buyer.name, size=10, bold=True)
    page.down(4.5 * mm)
    for value in (buyer.address, f"TIN {buyer.tin}" if buyer.tin else None, buyer.extra):
        if value:
            for part in value.splitlines():
                page.text(LEFT, part, colour=MUTED)
                page.down(4.2 * mm)
    page.down(4 * mm)


def render_document(doc: DocumentPdf) -> bytes:
    buffer = io.BytesIO()
    pdf = Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"{doc.title} {doc.number}")
    pdf.setAuthor(doc.seller.name)
    columns = (LEFT, 112 * mm, 138 * mm, 160 * mm, RIGHT)

    def table_head(page: _Page) -> None:
        for x, label in zip(
            columns, ("Description", "Qty", "Unit price", "VAT", "Amount"), strict=True
        ):
            page.text(
                x, label, size=8, bold=True, colour=MUTED, align="left" if x == LEFT else "right"
            )
        page.down(5 * mm)
        page.rule()

    def header(page: _Page) -> None:
        if page.pdf.getPageNumber() > 1:
            page.text(LEFT, f"{doc.title} {doc.number} (continued)", size=9, colour=MUTED)
            page.down(8 * mm)
            table_head(page)

    page = _Page(pdf, header)
    _parties(page, doc.title, doc.number, doc.seller, doc.buyer)
    for label, value in doc.facts:
        page.text(LEFT, label, colour=MUTED)
        page.text(LEFT + 40 * mm, value)
        page.down(4.5 * mm)
    page.down(5 * mm)
    table_head(page)
    for description, qty, price, vat, amount in doc.lines:
        page.text(LEFT, description[:60])
        for x, value in zip(columns[1:], (qty, price, vat, amount), strict=True):
            page.text(x, value, align="right")
        page.down(5.5 * mm)
    page.rule()
    page.down(2 * mm)
    for i, (label, value) in enumerate(doc.totals):
        last = i == len(doc.totals) - 1
        page.text(138 * mm, label, bold=last, align="right")
        page.text(RIGHT, value, bold=last, align="right")
        page.down(5.5 * mm)
    if doc.note:
        page.down(6 * mm)
        page.text(LEFT, doc.note, colour=MUTED)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def render_statement(doc: StatementPdf) -> bytes:
    buffer = io.BytesIO()
    pdf = Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Statement - {doc.buyer.name}")
    pdf.setAuthor(doc.seller.name)
    columns = (LEFT, 45 * mm, 70 * mm, 140 * mm, 165 * mm, RIGHT)

    def table_head(page: _Page) -> None:
        labels = ("Date", "Number", "Details", "Debit", "Credit", "Balance")
        for x, label in zip(columns, labels, strict=True):
            page.text(
                x,
                label,
                size=8,
                bold=True,
                colour=MUTED,
                align="right" if x >= 140 * mm else "left",
            )
        page.down(5 * mm)
        page.rule()

    def header(page: _Page) -> None:
        if page.pdf.getPageNumber() > 1:
            page.text(LEFT, f"Statement for {doc.buyer.name} (continued)", size=9, colour=MUTED)
            page.down(8 * mm)
            table_head(page)

    page = _Page(pdf, header)
    _parties(page, "Statement", doc.period, doc.seller, doc.buyer)
    table_head(page)
    page.text(70 * mm, "Opening balance")
    page.text(RIGHT, money(doc.opening), align="right")
    page.down(5.5 * mm)
    for row in doc.rows:
        for x, value in zip(columns, row, strict=True):
            right = x >= 140 * mm
            page.text(x, value if right else value[:40], align="right" if right else "left")
        page.down(5.5 * mm)
    page.rule()
    page.text(70 * mm, "Closing balance", bold=True)
    page.text(RIGHT, money(doc.closing), bold=True, align="right")
    page.down(10 * mm)
    width = (RIGHT - LEFT) / max(len(doc.aging), 1)
    for i, (label, _value) in enumerate(doc.aging):
        page.text(LEFT + i * width, label, size=8, colour=MUTED)
    page.down(4.5 * mm)
    for i, (_label, value) in enumerate(doc.aging):
        page.text(LEFT + i * width, value, bold=True)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
