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

# The migrations ARE the source of truth now — the module owns its schema and
# the numbered migrations in the platform repo are no longer the authority.
# db_pool applies 001 + 002 directly from here. 000 (the role) is NOT applied
# by the fixture: the RLS tests create their own probe role, and the dedicated
# 000 test exercises it in isolation. The role still has to exist for 001's
# GRANTs, so the fixture materialises it idempotently below (same statement as
# 000, run as a prerequisite rather than as a "migration").
MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_000 = MIGRATIONS / "000_create_role.sql"
MIGRATION_001 = MIGRATIONS / "001_agent_schema.sql"
MIGRATION_002 = MIGRATIONS / "002_agent_turn_audit.sql"

_ROLE_PRECONDITION = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_module') THEN
    CREATE ROLE agent_module LOGIN;
  END IF;
END $$;
"""

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
    pool = await asyncpg.create_pool(
        os.environ["POSTGRES_URL"],
        server_settings={"search_path": "agent_module,public"},
    )
    async with pool.acquire() as conn:
        await conn.execute(_ROLE_PRECONDITION)
        await conn.execute(MIGRATION_001.read_text())
        await conn.execute(MIGRATION_002.read_text())
        await conn.execute(
            "TRUNCATE agent_channel_links, agent_link_tokens, agent_processed_updates, "
            "agent_turn_audit"
        )
    yield pool
    await pool.close()
