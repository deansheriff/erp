"""Reading naira amounts typed into forms."""

from decimal import Decimal, InvalidOperation

from app.core.errors import ValidationFailed

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("999999999999.99")


def parse_money(value: str, field: str, *, blank: bool = False) -> Decimal | None:
    """A naira amount typed into a form ("12,500" or "12500.50"). Blank gives None when
    allowed."""
    text = value.strip().replace(",", "").replace("₦", "")
    if not text:
        if blank:
            return None
        raise ValidationFailed(field_errors={field: "Enter an amount."})
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValidationFailed(field_errors={field: "Enter an amount, like 12,500."}) from None
    if not amount.is_finite() or amount < 0 or amount > MAX_AMOUNT:
        raise ValidationFailed(field_errors={field: "Enter an amount, like 12,500."})
    if amount != amount.quantize(CENT):
        raise ValidationFailed(field_errors={field: "Use at most two decimal places (kobo)."})
    return amount.quantize(CENT)
