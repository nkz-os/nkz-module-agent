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

# Authored here so the module's CI — which checks out only this repo — can
# build a schema. The canonical numbered migration in the platform repo is a
# byte-identical copy; test_schema_matches_platform_migration guards the drift.
SCHEMA = pathlib.Path(__file__).resolve().parent / "fixtures" / "schema.sql"

requires_db = pytest.mark.skipif(
    not os.environ.get("POSTGRES_URL"),
    reason="POSTGRES_URL not set; start a PostgreSQL (see conftest docstring)",
)


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
