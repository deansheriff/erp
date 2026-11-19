"""The classed fields of the employee record (ADR 0017, docs/HR.md). Directory fields
(names, work email, status, branch) are handled by the person form; this module covers
the personal, statutory and banking fields."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app.authz.fields import FieldClass
from app.core.formatting import format_date
from app.modules.hr.models import EMPLOYMENT_TYPES, GENDERS, MARITAL_STATUSES

NIGERIAN_STATES = (
    "Abia",
    "Adamawa",
    "Akwa Ibom",
    "Anambra",
    "Bauchi",
    "Bayelsa",
    "Benue",
    "Borno",
    "Cross River",
    "Delta",
    "Ebonyi",
    "Edo",
    "Ekiti",
    "Enugu",
    "FCT",
    "Gombe",
    "Imo",
    "Jigawa",
    "Kaduna",
    "Kano",
    "Katsina",
    "Kebbi",
    "Kogi",
    "Kwara",
    "Lagos",
    "Nasarawa",
    "Niger",
    "Ogun",
    "Ondo",
    "Osun",
    "Oyo",
    "Plateau",
    "Rivers",
    "Sokoto",
    "Taraba",
    "Yobe",
    "Zamfara",
)

LABELS = {
    "permanent": "Permanent",
    "contract": "Contract",
    "intern": "Intern",
    "nysc": "NYSC corps member",
    "female": "Female",
    "male": "Male",
    "single": "Single",
    "married": "Married",
    "divorced": "Divorced",
    "widowed": "Widowed",
    "separated": "Separated",
}


def _choices(values: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((v, LABELS.get(v, v)) for v in values)


STATE_CHOICES = tuple((s, s) for s in NIGERIAN_STATES)

Validator = Callable[[str], str]  # returns the cleaned value or raises ValueError


def _pattern(regex: str, message: str) -> Validator:
    compiled = re.compile(regex)

    def check(value: str) -> str:
        cleaned = re.sub(r"\s+", "", value)
        if not compiled.fullmatch(cleaned):
            raise ValueError(message)
        return cleaned

    return check


def _phone(value: str) -> str:
    cleaned = " ".join(value.split())
    if not re.fullmatch(r"\+?[0-9 ()-]{7,20}", cleaned):
        raise ValueError("Enter a phone number, for example 0803 123 4567.")
    return cleaned


def _email(value: str) -> str:
    cleaned = value.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cleaned):
        raise ValueError("Enter an email address.")
    return cleaned


@dataclass(frozen=True, slots=True)
class HrField:
    name: str
    label: str
    field_class: FieldClass
    section: str
    kind: str = "text"  # text, textarea, date, select, email, phone
    choices: tuple[tuple[str, str], ...] = ()
    self_service: bool = False  # the employee may change it directly
    encrypted: bool = False
    max_length: int = 160
    validate: Validator | None = None
    hint: str = ""

    @property
    def column(self) -> str:
        return f"{self.name}_encrypted" if self.encrypted else self.name


P, S, B = FieldClass.PERSONAL, FieldClass.STATUTORY, FieldClass.BANKING

FIELDS: tuple[HrField, ...] = (
    # Employment (personal class, HR-managed)
    HrField(
        "employment_type",
        "Employment type",
        P,
        "employment",
        "select",
        _choices(EMPLOYMENT_TYPES),
    ),
    HrField("nysc_start", "NYSC service start", P, "employment", "date"),
    HrField("nysc_end", "NYSC service end", P, "employment", "date"),
    HrField("hire_date", "Hire date", P, "employment", "date"),
    HrField("probation_end", "Probation ends", P, "employment", "date"),
    HrField("confirmation_date", "Confirmation date", P, "employment", "date"),
    # Personal details (HR-managed)
    HrField("date_of_birth", "Date of birth", P, "personal", "date"),
    HrField("gender", "Gender", P, "personal", "select", _choices(GENDERS)),
    HrField("state_of_origin", "State of origin", P, "personal", "select", STATE_CHOICES),
    # Contact (self-service)
    HrField("phone", "Phone", P, "contact", "phone", self_service=True, validate=_phone),
    HrField(
        "personal_email",
        "Personal email",
        P,
        "contact",
        "email",
        self_service=True,
        max_length=254,
        validate=_email,
    ),
    HrField(
        "residential_address",
        "Residential address",
        P,
        "contact",
        "textarea",
        self_service=True,
        max_length=400,
    ),
    HrField(
        "state_of_residence",
        "State of residence",
        P,
        "contact",
        "select",
        STATE_CHOICES,
        self_service=True,
        hint="Used for PAYE remittance.",
    ),
    HrField(
        "marital_status",
        "Marital status",
        P,
        "contact",
        "select",
        _choices(MARITAL_STATUSES),
        self_service=True,
    ),
    # Next of kin (self-service)
    HrField("next_of_kin_name", "Name", P, "next_of_kin", self_service=True),
    HrField(
        "next_of_kin_relationship",
        "Relationship",
        P,
        "next_of_kin",
        self_service=True,
        max_length=60,
    ),
    HrField(
        "next_of_kin_phone", "Phone", P, "next_of_kin", "phone", self_service=True, validate=_phone
    ),
    HrField(
        "next_of_kin_address",
        "Address",
        P,
        "next_of_kin",
        "textarea",
        self_service=True,
        max_length=400,
    ),
    # Statutory
    HrField(
        "nin",
        "NIN",
        S,
        "statutory",
        encrypted=True,
        validate=_pattern(r"\d{11}", "The NIN is 11 digits."),
    ),
    HrField(
        "tin",
        "TIN",
        S,
        "statutory",
        encrypted=True,
        validate=_pattern(r"[0-9-]{8,15}", "Enter the TIN: 8 to 15 digits, hyphens allowed."),
    ),
    HrField("pension_pfa", "Pension fund administrator", S, "statutory", max_length=120),
    HrField(
        "rsa_pin",
        "RSA PIN",
        S,
        "statutory",
        encrypted=True,
        validate=_pattern(r"PEN\d{12}", "The RSA PIN is PEN followed by 12 digits."),
    ),
    HrField(
        "nhf_number",
        "NHF number",
        S,
        "statutory",
        encrypted=True,
        validate=_pattern(r"[A-Za-z0-9/-]{4,20}", "Enter the NHF number (letters and digits)."),
    ),
    # Banking
    HrField("bank_name", "Bank", B, "banking", max_length=120),
    HrField(
        "account_number",
        "Account number",
        B,
        "banking",
        encrypted=True,
        validate=_pattern(r"\d{10}", "The account number (NUBAN) is 10 digits."),
    ),
    HrField("account_name", "Account name", B, "banking"),
    HrField(
        "bvn",
        "BVN",
        B,
        "banking",
        encrypted=True,
        validate=_pattern(r"\d{11}", "The BVN is 11 digits."),
    ),
)

BY_NAME = {f.name: f for f in FIELDS}

SECTIONS: tuple[tuple[str, str, FieldClass], ...] = (
    ("employment", "Employment", P),
    ("personal", "Personal details", P),
    ("contact", "Contact", P),
    ("next_of_kin", "Next of kin", P),
    ("statutory", "Statutory", S),
    ("banking", "Bank", B),
)

# Classes whose values the audit log never records.
MASKED = {f.name for f in FIELDS if f.field_class.masked_in_audit}


def fields_of(field_class: FieldClass) -> tuple[HrField, ...]:
    return tuple(f for f in FIELDS if f.field_class is field_class)


def clean(field: HrField, raw: str) -> str | date | None:
    """Validate one submitted value. Blank means "no value". Raises ValueError with a
    message for the person."""
    value = raw.strip()
    if not value:
        return None
    if len(value) > field.max_length:
        raise ValueError(f"Keep this under {field.max_length} characters.")
    if field.kind == "date":
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Enter a date.") from exc
    if field.kind == "select" and value not in {v for v, _ in field.choices}:
        raise ValueError("Choose one of the options.")
    if field.validate is not None:
        return field.validate(value)
    return " ".join(value.split()) if field.kind != "textarea" else value


def display(field: HrField, value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, date):
        return format_date(value)
    if field.kind == "select":
        return dict(field.choices).get(str(value), str(value))
    return str(value)


def mask(value: str) -> str:
    """Last four characters only, for request comparisons."""
    return "••••" + value[-4:] if len(value) > 4 else "••••"
