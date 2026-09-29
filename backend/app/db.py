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
        # The module owns its tables in the dedicated ``agent_module`` schema;
        # the queries throughout app/ are intentionally unqualified, so the
        # schema is resolved here rather than at every call site. Without this,
        # a plain connection's default search_path (``"$user", public``) would
        # not see them.
        # The module owns its tables in the dedicated ``agent_module`` schema;
        # the queries throughout app/ are intentionally unqualified, so the
        # schema is resolved here rather than at every call site. Without this,
        # a plain connection's default search_path (``"$user", public``) would
        # not see them.
        _pool = await asyncpg.create_pool(
            require_postgres_url(),
            min_size=1,
            max_size=10,
            server_settings={"search_path": "agent_module,public"},
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
