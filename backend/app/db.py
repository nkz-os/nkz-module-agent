"""PostgreSQL connection pool.

Admin/metadata only (channel links, link tokens, update dedupe). Never
time-series: that data reaches TimescaleDB through Orion-LD subscriptions.
"""

from __future__ import annotations

import asyncpg

from app.config import require_postgres_url

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the process-wide pool, creating it on first use."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(require_postgres_url(), min_size=1, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
