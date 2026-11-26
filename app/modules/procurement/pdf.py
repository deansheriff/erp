"""The request for quotation PDF sent to each supplier (ADR 0030), drawn on demand with
reportlab's built-in fonts."""

import io
from dataclasses import dataclass
from datetime import date

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

INK = (0.086, 0.086, 0.165)
MUTED = (0.384, 0.384, 0.502)
LINE = (0.894, 0.894, 0.933)
WIDTH, HEIGHT = A4
LEFT, RIGHT = 20 * mm, WIDTH - 20 * mm
BOTTOM = 25 * mm


@dataclass(frozen=True, slots=True)
class RfqLine:
    description: str
    quantity: str
    unit: str


@dataclass(frozen=True, slots=True)
class RfqDocument:
    organisation: str
    number: str
    issued: date
    respond_by: date | None
    supplier: str
    supplier_address: str | None
    note: str | None
    lines: list[RfqLine]


def _fit(canvas: Canvas, text: str, font: str, size: float, width: float) -> str:
    if canvas.stringWidth(text, font, size) <= width:
        return text
    while text and canvas.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def _header(canvas: Canvas, doc: RfqDocument) -> float:
    canvas.setFillColorRGB(*INK)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(LEFT, HEIGHT - 25 * mm, doc.organisation)
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawRightString(RIGHT, HEIGHT - 25 * mm, "Request for quotation")
    canvas.setFont("Helvetica", 10)
    canvas.setFillColorRGB(*MUTED)
    canvas.drawRightString(RIGHT, HEIGHT - 31 * mm, doc.number)
    y = HEIGHT - 45 * mm
    canvas.setFillColorRGB(*INK)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(LEFT, y, "To")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(LEFT, y - 5 * mm, doc.supplier)
    for n, part in enumerate((doc.supplier_address or "").splitlines()[:3], start=2):
        canvas.drawString(LEFT, y - n * 5 * mm, part)
    canvas.setFont("Helvetica", 10)
    canvas.drawRightString(RIGHT, y, f"Issued {doc.issued:%d %b %Y}")
    if doc.respond_by:
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawRightString(RIGHT, y - 5 * mm, f"Please reply by {doc.respond_by:%d %b %Y}")
    return float(y - 30 * mm)


def draw(doc: RfqDocument) -> bytes:
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=A4)
    canvas.setTitle(f"Request for quotation {doc.number}")
    y = _header(canvas, doc)
    canvas.setFont("Helvetica", 10)
    canvas.setFillColorRGB(*INK)
    canvas.drawString(
        LEFT,
        y,
        "Please quote a unit price excluding VAT for each line, the VAT that applies,",
    )
    canvas.drawString(LEFT, y - 5 * mm, "delivery time in days and how long your prices hold.")
    y -= 15 * mm
    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColorRGB(*MUTED)
    canvas.drawString(LEFT, y, "#")
    canvas.drawString(LEFT + 10 * mm, y, "Item")
    canvas.drawRightString(RIGHT - 25 * mm, y, "Quantity")
    canvas.drawString(RIGHT - 22 * mm, y, "Unit")
    canvas.setStrokeColorRGB(*LINE)
    canvas.line(LEFT, y - 2.5 * mm, RIGHT, y - 2.5 * mm)
    y -= 8 * mm
    canvas.setFillColorRGB(*INK)
    for n, line in enumerate(doc.lines, start=1):
        if y < BOTTOM + 20 * mm:
            canvas.showPage()
            y = HEIGHT - 25 * mm
        canvas.setFont("Helvetica", 10)
        canvas.drawString(LEFT, y, str(n))
        canvas.drawString(
            LEFT + 10 * mm, y, _fit(canvas, line.description, "Helvetica", 10, 110 * mm)
        )
        canvas.drawRightString(RIGHT - 25 * mm, y, line.quantity)
        canvas.drawString(RIGHT - 22 * mm, y, line.unit)
        y -= 7 * mm
    if doc.note:
        y -= 5 * mm
        canvas.setFont("Helvetica", 10)
        for part in doc.note.splitlines()[:6]:
            canvas.drawString(LEFT, y, _fit(canvas, part, "Helvetica", 10, RIGHT - LEFT))
            y -= 5 * mm
    canvas.setFont("Helvetica", 8)
    canvas.setFillColorRGB(*MUTED)
    canvas.drawString(LEFT, BOTTOM - 10 * mm, f"Quote {doc.number} in your reply.")
    canvas.save()
    return buffer.getvalue()
