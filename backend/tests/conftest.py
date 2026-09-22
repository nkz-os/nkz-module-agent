"""Test fixtures.

Integration tests need a real PostgreSQL: the behaviour under test (partial
unique indexes, atomic single-use updates, ON CONFLICT races) does not exist
in a fake.

Start one with:
    docker run -d --name agent-test-db -p 55432:5432 \
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test postgres:15
    export POSTGRES_URL=postgresql://postgres:test@localhost:55432/test
"""

from __future__ import annotations

import os
import pathlib

import asyncpg
import pytest
import pytest_asyncio

from app.db import close_pool

# Authored here so the module's CI — which checks out only this repo — can
# build a schema. The canonical numbered migration in the platform repo is a
# byte-identical copy; test_schema_matches_platform_migration guards the drift.
SCHEMA = pathlib.Path(__file__).resolve().parent / "fixtures" / "schema.sql"

requires_db = pytest.mark.skipif(
    not os.environ.get("POSTGRES_URL"),
    reason="POSTGRES_URL not set; start a PostgreSQL (see conftest docstring)",
)


@pytest_asyncio.fixture(autouse=True)
async def _reset_process_pool():
    """Reset app.db's process-wide pool around every test.

    app.db.get_pool() is a process-wide singleton by design: one pool for
    the whole running service. But pytest-asyncio gives each test function
    its own event loop, and an asyncpg pool is bound to the loop that
    created it. Left alone, a pool created by one test's loop is unusable —
    and produces confusing "attached to a different loop" / "event loop is
    closed" errors — once a later test's loop tries to use it. Closing it
    before and after every test forces get_pool() to lazily build a fresh,
    loop-correct pool on demand. close_pool() is a no-op when no pool
    exists yet, so tests that never touch the database are unaffected.
    """
    await close_pool()
    yield
    await close_pool()


@pytest_asyncio.fixture
async def db_pool():
    pool = await asyncpg.create_pool(os.environ["POSTGRES_URL"])
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA.read_text())
        await conn.execute(
            "TRUNCATE agent_channel_links, agent_link_tokens, agent_processed_updates"
        )
    yield pool
    await pool.close()
