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


@requires_db
@pytest.mark.asyncio
async def test_row_security_is_enabled_and_forced(db_pool):
    """FORCE is the half people forget.

    Without it the table owner is exempt, and the owner is the role that runs
    migrations -- so the policy would be there and do nothing for the one role
    most likely to be reused by a service.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'agent_turn_audit'"
        )
        assert row["relrowsecurity"] is True
        assert row["relforcerowsecurity"] is True


@requires_db
@pytest.mark.asyncio
async def test_tenant_policy_exists(db_pool):
    async with db_pool.acquire() as conn:
        names = [
            r["polname"]
            for r in await conn.fetch(
                "SELECT polname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                "WHERE c.relname = 'agent_turn_audit'"
            )
        ]
        assert names == ["agent_turn_audit_tenant_isolation"]


@requires_db
@pytest.mark.asyncio
async def test_policy_actually_isolates_a_non_superuser_role(db_pool):
    """The only test here that proves protection rather than configuration.

    A superuser bypasses row security unconditionally -- FORCE included -- so
    asserting the policy exists says nothing about whether it constrains
    anyone. This creates a role that is neither owner nor superuser, which is
    what the service is meant to connect as, and checks the three things that
    matter: it sees its own tenant, it does not see another's, and it cannot
    write a row belonging to another.
    """
    async with db_pool.acquire() as conn:
        await conn.execute("DROP ROLE IF EXISTS agent_rls_probe")
        await conn.execute("CREATE ROLE agent_rls_probe NOSUPERUSER")
        try:
            await conn.execute(
                "GRANT SELECT, INSERT ON agent_turn_audit TO agent_rls_probe"
            )
            # Without the sequence grant the INSERT below fails on permissions
            # before the policy is ever consulted -- a pass for the wrong
            # reason, which is why the assertion matches the error text.
            await conn.execute(
                "GRANT USAGE, SELECT ON SEQUENCE agent_turn_audit_id_seq "
                "TO agent_rls_probe"
            )
            await conn.execute(
                "INSERT INTO agent_turn_audit "
                "(trace_id, tenant_id, user_id, channel, channel_user_id, outcome) "
                "VALUES ('a','alpha','u','telegram','1','ok'), "
                "       ('b','beta','u','telegram','2','ok')"
            )
            await conn.execute("SET ROLE agent_rls_probe")
            await conn.execute("SET app.current_tenant = 'alpha'")

            assert await conn.fetchval("SELECT count(*) FROM agent_turn_audit") == 1
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_turn_audit WHERE tenant_id <> 'alpha'"
                )
                == 0
            )

            with pytest.raises(Exception, match="row-level security"):
                await conn.execute(
                    "INSERT INTO agent_turn_audit "
                    "(trace_id, tenant_id, user_id, channel, channel_user_id, outcome) "
                    "VALUES ('x','beta','u','telegram','3','ok')"
                )
        finally:
            await conn.execute("RESET ROLE")
            await conn.execute(
                "REVOKE ALL ON agent_turn_audit FROM agent_rls_probe"
            )
            await conn.execute(
                "REVOKE ALL ON SEQUENCE agent_turn_audit_id_seq "
                "FROM agent_rls_probe"
            )
            await conn.execute("DROP ROLE IF EXISTS agent_rls_probe")
