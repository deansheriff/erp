"""Running one assistant request (ADR 0035): check the feature is on and the person is
under their cap, call the provider, check every quote against its source, keep the
request in the person's history, and audit what was sent (never the text)."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.audit.service import record_by
from app.core.errors import Conflict
from app.core.secrets import DecryptionFailed, decrypt
from app.identity.schemas import Actor
from app.modules.assistant import common, providers
from app.modules.assistant.common import Source
from app.modules.assistant.models import AssistantRequest


@dataclass(frozen=True, slots=True)
class Ask:
    feature: str
    title: str  # for the history list
    inputs: dict[str, Any]  # what the person asked, kept in their history
    system: str
    prompt: str
    schema: dict[str, Any]
    sources: list[Source]
    # Checks the reply beyond its citations; returns an error to show, or None.
    check: Callable[[dict[str, Any]], str | None] | None = None


def provider_for(db: Session) -> providers.Provider:
    row = common.settings(db)
    try:
        key = decrypt(row.api_key_encrypted) if row.api_key_encrypted else ""
    except DecryptionFailed as exc:
        raise Conflict(
            "The provider's API key can't be read. Enter it again in the settings."
        ) from exc
    if not key:
        raise Conflict("No API key is saved. A Super Administrator can add one in the settings.")
    return providers.build(row.provider, row.model, key)


def run(db: Session, actor: Actor, ask: Ask) -> uuid.UUID:
    """The request's id, once it's stored. Failures to reach the provider raise Conflict
    and aren't counted against the cap."""
    common.require(db, actor, common.FEATURE_PERMISSIONS[ask.feature])
    problem = common.available(db, ask.feature)
    if problem:
        raise Conflict(problem)
    common.check_cap(db, actor)
    row = common.settings(db)
    try:
        reply = provider_for(db).generate(
            system=ask.system,
            prompt=ask.prompt,
            schema=ask.schema,
            name=f"kx_{ask.feature}",
        )
    except providers.ProviderError as exc:
        record_by(
            db,
            actor,
            "assistant.request.failed",
            entity_type="assistant",
            entity_id=ask.feature,
            changes={"provider": row.provider, "model": row.model},
        )
        raise Conflict(exc.message) from exc
    body, status = _checked(reply, ask)
    request = AssistantRequest(
        user_id=actor.user_id,
        feature=ask.feature,
        title=ask.title[:200],
        inputs=ask.inputs,
        reply=body,
        status=status,
        provider=row.provider,
        model=row.model,
        kinds_sent=list(common.NEEDS[ask.feature]),
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )
    db.add(request)
    db.flush()
    record_by(
        db,
        actor,
        "assistant.request",
        entity_type="assistant_request",
        entity_id=str(request.id),
        changes={
            "feature": ask.feature,
            "provider": row.provider,
            "model": row.model,
            "kinds_sent": list(common.NEEDS[ask.feature]),
            "sources": len(ask.sources),
            "status": status,
        },
    )
    return request.id


def _checked(reply: providers.Reply, ask: Ask) -> tuple[dict[str, Any], str]:
    if reply.refused or reply.data is None:
        return {"message": "The provider declined to answer this request."}, "refused"
    data = dict(reply.data)
    checked = common.check_citations(data.pop("citations", []), ask.sources)
    body = {
        **data,
        "citations": checked.citations,
        "dropped": checked.dropped,
        # What was sent, for the person to see beside the reply.
        "sources": [
            {"key": s.key, "label": s.label, "text": s.text, "link": s.link} for s in ask.sources
        ],
    }
    if ask.check is not None:
        error = ask.check(body)
        if error:
            return {"message": error}, "refused"
    # Answers from sources must be backed by a checked quote, unless they say nothing was
    # found. Drafts (job descriptions, ticket replies) needn't quote anything.
    needs_quote = ask.feature in ("policy", "evidence") and data.get("found", True) is not False
    status = "unverified" if needs_quote and not checked.citations else "ok"
    return body, status
