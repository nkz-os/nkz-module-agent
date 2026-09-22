"""Agent management routes.

These arrive through the api-gateway, which has already authenticated the
user, so they trust the injected X-Tenant-ID / X-User-ID / X-User-Roles
headers via require_auth(). The channel webhook does NOT: see api/webhook.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from nkz_platform_sdk.auth import AuthContext
from pydantic import BaseModel

from app.config import get_settings
from app.identity import repository as repo
from app.identity import service
from app.middleware import get_current_user

router = APIRouter(tags=["agent"])


class LinkTokenResponse(BaseModel):
    deep_link: str
    expires_in: int


class LinkItem(BaseModel):
    id: int
    channel: str
    channel_user_id: str
    linked_at: str
    last_seen_at: str | None


class LinkListResponse(BaseModel):
    links: list[LinkItem]


@router.post("/link-tokens", response_model=LinkTokenResponse)
async def create_link_token(
    user: AuthContext = Depends(get_current_user),
) -> LinkTokenResponse:
    """Mint a single-use deep link that connects a chat account to this user."""
    _, deep_link = await service.create_link_token(
        user.tenant_id, user.user_id, tuple(user.roles)
    )
    return LinkTokenResponse(
        deep_link=deep_link, expires_in=get_settings().link_token_ttl_seconds
    )


@router.get("/links", response_model=LinkListResponse)
async def list_links(
    user: AuthContext = Depends(get_current_user),
) -> LinkListResponse:
    rows = await repo.list_links(user.tenant_id, user.user_id)
    return LinkListResponse(
        links=[
            LinkItem(
                id=r["id"],
                channel=r["channel"],
                channel_user_id=r["channel_user_id"],
                linked_at=r["linked_at"].isoformat(),
                last_seen_at=r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            )
            for r in rows
        ]
    )


@router.delete("/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_link(
    link_id: int, user: AuthContext = Depends(get_current_user)
) -> Response:
    """Revoke a link. Scoped to the caller's tenant, so a foreign id is a 404."""
    if not await repo.revoke_link(link_id, user.tenant_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
