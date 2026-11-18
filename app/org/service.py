"""The organisation record (one per installation, ADR 0002)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.org.models import Organization
from app.org.schemas import OrganizationInput, OrganizationView


def get_organization(db: Session) -> OrganizationView | None:
    row = db.scalars(select(Organization)).one_or_none()
    if row is None:
        return None
    return OrganizationView(
        legal_name=row.legal_name,
        trading_name=row.trading_name,
        rc_number=row.rc_number,
        tin=row.tin,
        address=row.address,
        timezone=row.timezone,
        base_currency=row.base_currency,
        fiscal_year_start_month=row.fiscal_year_start_month,
    )


def save_organization(db: Session, data: OrganizationInput) -> None:
    row = db.scalars(select(Organization)).one_or_none()
    if row is None:
        row = Organization(legal_name=data.legal_name)
        db.add(row)
    row.legal_name = data.legal_name.strip()
    row.trading_name = data.trading_name
    row.rc_number = data.rc_number
    row.tin = data.tin
    row.address = data.address
    row.timezone = data.timezone
    row.base_currency = data.base_currency.upper()
    row.fiscal_year_start_month = data.fiscal_year_start_month
    db.flush()
