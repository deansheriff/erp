"""Renders email bodies from app/mail/templates (plain text and HTML)."""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)

SUBJECTS = {
    "invite": "You're invited to {organisation} on Kxmeleon",
    "password_reset": "Reset your Kxmeleon password",
    "test": "Kxmeleon test email",
    "file_quarantined": "A file was blocked by the virus scanner",
    "finance_payment_reminder": "Payment reminder from {organisation}",
    "finance_sales_document": "{kind_title} {number} from {organisation}",
    "inventory_low_stock": "Low stock at {organisation}: {count} to reorder",
}


def render(template: str, context: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, text, html)."""
    subject = SUBJECTS[template].format(**context)
    text = _env.get_template(f"{template}.txt").render(**context)
    html = _env.get_template(f"{template}.html").render(subject=subject, **context)
    return subject, text, html
