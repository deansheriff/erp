"""JSON API for identity. Callable with a session or an `Authorization: Bearer kx_…` key."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.authz.declarations import requires_login
from app.identity.schemas import Actor
from app.web.access import require_actor

router = APIRouter(prefix="/api/v1", tags=["identity"])


@router.get("/me")
@requires_login
def me(actor: Annotated[Actor, Depends(require_actor)]) -> dict[str, str]:
    """Who am I? Useful to check an API key works."""
    return {
        "id": str(actor.user_id),
        "email": actor.email,
        "full_name": actor.full_name,
        "authenticated_with": actor.via,
    }
