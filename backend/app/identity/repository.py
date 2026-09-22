"""SQL for channel links and link tokens. No business rules here."""

from __future__ import annotations

from datetime import datetime

from app.db import get_pool


async def insert_link_token(
    token_hash: str,
    tenant_id: str,
    user_id: str,
    roles: tuple[str, ...],
    expires_at: datetime,
) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO agent_link_tokens (token_hash, tenant_id, user_id, roles, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        """,
        token_hash, tenant_id, user_id, list(roles), expires_at,
    )


async def consume_link_token(token_hash: str) -> dict | None:
    """Atomically mark a token consumed and return its identity.

    Unknown, expired and already-consumed tokens are indistinguishable to the
    caller: all three yield None.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE agent_link_tokens
           SET consumed_at = now()
         WHERE token_hash = $1
           AND consumed_at IS NULL
           AND expires_at > now()
        RETURNING tenant_id, user_id, roles
        """,
        token_hash,
    )
    return dict(row) if row else None


async def upsert_active_link(
    channel: str,
    channel_user_id: str,
    tenant_id: str,
    user_id: str,
    roles: tuple[str, ...],
) -> int:
    """Revoke any active link for this channel account, then insert the new one.

    Both statements share a transaction so the partial unique index is never
    violated and no window exists where the account has two active links.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE agent_channel_links
                   SET status = 'revoked', revoked_at = now()
                 WHERE channel = $1 AND channel_user_id = $2 AND status = 'active'
                """,
                channel, channel_user_id,
            )
            return await conn.fetchval(
                """
                INSERT INTO agent_channel_links
                       (channel, channel_user_id, tenant_id, user_id, roles)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                channel, channel_user_id, tenant_id, user_id, list(roles),
            )


async def get_active_link(channel: str, channel_user_id: str) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, tenant_id, user_id, roles
          FROM agent_channel_links
         WHERE channel = $1 AND channel_user_id = $2 AND status = 'active'
        """,
        channel, channel_user_id,
    )
    return dict(row) if row else None


async def revoke_link(link_id: int, tenant_id: str) -> bool:
    """Revoke a link. Scoped by tenant so one tenant cannot revoke another's."""
    pool = await get_pool()
    result = await pool.execute(
        """
        UPDATE agent_channel_links
           SET status = 'revoked', revoked_at = now()
         WHERE id = $1 AND tenant_id = $2 AND status = 'active'
        """,
        link_id, tenant_id,
    )
    return result.endswith(" 1")


async def list_links(tenant_id: str, user_id: str) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, channel, channel_user_id, linked_at, last_seen_at
          FROM agent_channel_links
         WHERE tenant_id = $1 AND user_id = $2 AND status = 'active'
         ORDER BY linked_at DESC
        """,
        tenant_id, user_id,
    )
    return [dict(r) for r in rows]
