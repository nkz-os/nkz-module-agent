"""Schema tests for agent_turn_audit (migration 102), against a real PostgreSQL.

db_pool already applies fixtures/schema_102.sql once during setup (see
conftest.py). These tests exercise the two properties a shell harness would
otherwise check by hand: that re-applying the migration is a no-op (not an
error), and that the columns a turn write depends on are actually there.
"""

import pytest

from tests.conftest import SCHEMA_102, requires_db


@requires_db
@pytest.mark.asyncio
async def test_migration_is_idempotent(db_pool):
    """db_pool's fixture setup already applied schema_102.sql once; applying
    it again here, on the same connection, must not raise. If a CREATE TABLE
    or CREATE INDEX in the schema ever loses its IF NOT EXISTS guard, this is
    what turns that into a red test instead of a silent prod migration failure."""
    async with db_pool.acquire() as conn:
        await conn.execute(SCHEMA_102.read_text())


@requires_db
@pytest.mark.asyncio
async def test_required_columns_exist(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            SELECT trace_id, tenant_id, user_id, channel, channel_user_id,
                   inbound_text, model, tool_calls, reply_text, tokens_prompt,
                   tokens_completion, latency_ms, outcome, created_at
            FROM agent_turn_audit WHERE false
            """
        )


@requires_db
@pytest.mark.asyncio
async def test_row_can_be_written_with_the_fields_a_turn_produces(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_turn_audit
            (trace_id, tenant_id, user_id, channel, channel_user_id, inbound_text,
             model, tool_calls, reply_text, tokens_prompt, tokens_completion,
             latency_ms, outcome)
            VALUES ('t-1','tenant_a','user_a','telegram','42','hola',
                    'test-model','[]'::jsonb,'hola',10,5,120,'ok')
            """
        )
        row = await conn.fetchrow("SELECT outcome FROM agent_turn_audit WHERE trace_id = 't-1'")
        assert row["outcome"] == "ok"
