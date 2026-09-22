"""Account linking rules.

A link token is a bearer credential with a short life and a single use. It is
minted only for an already-authenticated platform session, so redeeming it
binds a channel account to an identity the platform already verified.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.domain.session import SessionContext
from app.identity import repository as repo

# 32 bytes of entropy renders as 43 urlsafe characters, inside Telegram's
# 64-character /start payload limit, using only characters it accepts.
_TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_link_token(
    tenant_id: str, user_id: str, roles: tuple[str, ...]
) -> tuple[str, str]:
    """Mint a link token. Returns (clear token, deep link).

    The clear token is returned to the caller and never persisted.
    """
    settings = get_settings()
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.link_token_ttl_seconds
    )
    await repo.insert_link_token(hash_token(token), tenant_id, user_id, roles, expires_at)
    deep_link = f"https://t.me/{settings.telegram_bot_username}?start={token}"
    return token, deep_link


async def redeem_link_token(
    token: str, channel: str, channel_user_id: str
) -> SessionContext | None:
    """Exchange a token for an active link. Returns None on any failure."""
    identity = await repo.consume_link_token(hash_token(token))
    if identity is None:
        return None

    roles = tuple(identity["roles"])
    await repo.upsert_active_link(
        channel, channel_user_id, identity["tenant_id"], identity["user_id"], roles
    )
    return SessionContext(
        tenant_id=identity["tenant_id"],
        user_id=identity["user_id"],
        roles=roles,
        channel=channel,
        channel_user_id=channel_user_id,
        trace_id="link",
    )


async def resolve_session(
    channel: str, channel_user_id: str, trace_id: str
) -> SessionContext | None:
    """Resolve a channel account to a session. None means: not linked."""
    link = await repo.get_active_link(channel, channel_user_id)
    if link is None:
        return None
    return SessionContext(
        tenant_id=link["tenant_id"],
        user_id=link["user_id"],
        roles=tuple(link["roles"]),
        channel=channel,
        channel_user_id=channel_user_id,
        trace_id=trace_id,
    )
