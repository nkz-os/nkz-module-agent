"""Schema tests for agent_mobile_turns (migrations/005), against a real PostgreSQL."""

import asyncpg
import pytest

from tests.conftest import requires_db

EXPECTED_COLUMNS = {
    "turn_id", "tenant_id", "user_id", "client_turn_id", "parcel_id", "lang",
    "text", "state", "reply_text", "citations", "outcome", "error_code",
    "trace_id", "created_at", "started_at", "finished_at",
}

_INSERT = (
    "INSERT INTO agent_module.agent_mobile_turns "
    "(tenant_id, user_id, client_turn_id, text, state, trace_id) "
    "VALUES ($1, $2, $3, 'hello', $4, 'trace-1')"
)


@requires_db
@pytest.mark.asyncio
async def test_columns_are_exactly_the_contract(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'agent_module' AND table_name = 'agent_mobile_turns'"
        )
    assert {r["column_name"] for r in rows} == EXPECTED_COLUMNS


@requires_db
@pytest.mark.asyncio
async def test_defaults(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(_INSERT, "acme", "u1", "c1", "queued")
        row = await conn.fetchrow(
            "SELECT turn_id, lang, citations::text AS citations, created_at "
            "FROM agent_module.agent_mobile_turns"
        )
    assert row["turn_id"] is not None
    assert row["lang"] == "es"
    assert row["citations"] == "[]"
    assert row["created_at"] is not None


@requires_db
@pytest.mark.asyncio
async def test_client_turn_id_unique_per_tenant_and_user(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(_INSERT, "acme", "u1", "c1", "queued")
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(_INSERT, "acme", "u1", "c1", "queued")
        # Same client id for another user or tenant is a different turn.
        await conn.execute(_INSERT, "acme", "u2", "c1", "queued")
        await conn.execute(_INSERT, "tenant-a", "u1", "c1", "queued")


@requires_db
@pytest.mark.asyncio
async def test_state_check_rejects_unknown_value(db_pool):
    async with db_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(_INSERT, "acme", "u1", "c1", "pending")
        for state in ("queued", "running", "done", "failed"):
            await conn.execute(_INSERT, "acme", "u1", f"ok-{state}", state)
