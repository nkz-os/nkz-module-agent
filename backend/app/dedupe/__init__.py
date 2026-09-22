"""Inbound update dedupe.

Messaging platforms retry deliveries that are not acknowledged quickly. Without
a claim step, a retry runs the whole turn again: duplicated cost now, and
duplicated writes once mutations exist.
"""

from __future__ import annotations

from app.config import get_settings
from app.db import get_pool


async def claim_update(idempotency_key: str) -> bool:
    """Claim an update for processing.

    Returns True exactly once per key, even under concurrent delivery: the
    insert either wins or conflicts, decided by the primary key.
    """
    pool = await get_pool()
    claimed = await pool.fetchval(
        """
        INSERT INTO agent_processed_updates (idempotency_key)
        VALUES ($1)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING idempotency_key
        """,
        idempotency_key,
    )
    return claimed is not None


async def purge_expired() -> int:
    """Drop dedupe rows past their retention window. Returns rows removed."""
    pool = await get_pool()
    hours = get_settings().dedupe_ttl_hours
    result = await pool.execute(
        "DELETE FROM agent_processed_updates "
        "WHERE seen_at < now() - ($1 || ' hours')::interval",
        str(hours),
    )
    return int(result.rsplit(" ", 1)[1])
