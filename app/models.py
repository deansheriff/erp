"""Imports every model module so Base.metadata is complete (Alembic, schema tests).

Add each new module's models here.
"""

from app.audit.models import AuditLog

__all__ = ["AuditLog"]
