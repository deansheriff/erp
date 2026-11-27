"""File purpose for the policy library (ADR 0035): each version's file is attached to its
policy. HR attaches; every signed-in person may read policies."""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.authz.engine import Resource
from app.files.purposes import MB, FilePurpose
from app.modules.assistant.models import Policy

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _policy(db: Session, entity_id: str) -> Resource | None:
    try:
        key = uuid.UUID(entity_id)
    except ValueError:
        return None
    return Resource() if db.get(Policy, key) is not None else None


POLICY_FILE = FilePurpose(
    key="assistant_policy",
    label="Policy document",
    entity_type="assistant_policy",
    allowed_types=frozenset({"application/pdf", DOCX, "text/plain"}),
    max_bytes=10 * MB,
    attach_permission="assistant.policy.manage",
    view_permission=None,  # policies are for everyone to read
    # Kept six years after a version is replaced (confirm).
    retention=timedelta(days=6 * 365),
    inline=True,
    classification="directory",
    resolve=_policy,
)

PURPOSES = (POLICY_FILE,)
