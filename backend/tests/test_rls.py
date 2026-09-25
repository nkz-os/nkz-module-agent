"""RLS functional tests for the agent module.

``agent_turn_audit`` (fixtures/schema_102.sql) has row-level security enabled
and FORCEd, reading the tenant from the ``app.current_tenant`` session setting.
A superuser bypasses RLS unconditionally, so the schema tests that assert
policy configuration cannot prove the *service* is protected — only a role
that is neither owner nor superuser is actually constrained.

These tests create that role and run the real code paths (``record_turn`` and
``check_and_count``) against it through a dedicated pool whose connections
``SET ROLE`` to it on creation, following the pattern already used by
``test_agent_turn_audit_schema.py``. They prove the two things the RLS rollout
turns from inert to load-bearing:

* the audit INSERT and the quota SELECTs set the tenant context before touching
  the table, so a least-privilege role can read and write its own tenant;
* the context is SET LOCAL — it dies with the transaction, so a recycled pool
  connection cannot leak one tenant's context into another tenant's turn.
"""

import os

import asyncpg
import pytest

from app.agent.loop import TurnResult
from app.agent.quota import check_and_count
from app.audit.repository import record_turn
from app.config import get_settings
from app.domain.session import SessionContext
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

ROLE = "agent_rls_probe"


def _ctx(tenant_id="tenant_a", trace_id="t-rls") -> SessionContext:
    return SessionContext(
        tenant_id=tenant_id, user_id="u", roles=("Farmer",),
        channel="telegram", channel_user_id="42", trace_id=trace_id,
    )


def _result() -> TurnResult:
    return TurnResult(text="hi", outcome="ok", model="m", tool_calls=(),
                      tokens_prompt=10, tokens_completion=5)


async def _create_probe_role(db_pool) -> None:
    async with db_pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE):
            # Leftover role (e.g. a failed earlier run): drop its grants first,
            # otherwise the DROP below fails on dependent privileges.
            await conn.execute(f"REVOKE ALL ON agent_turn_audit FROM {ROLE}")
            await conn.execute(
                f"REVOKE ALL ON SEQUENCE agent_turn_audit_id_seq FROM {ROLE}"
            )
            await conn.execute(f"DROP ROLE {ROLE}")
        await conn.execute(f"CREATE ROLE {ROLE} NOSUPERUSER")
        await conn.execute(
            f"GRANT SELECT, INSERT ON agent_turn_audit TO {ROLE}"
        )
        await conn.execute(
            f"GRANT USAGE, SELECT ON SEQUENCE agent_turn_audit_id_seq TO {ROLE}"
        )


async def _drop_probe_role(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(f"REVOKE ALL ON agent_turn_audit FROM {ROLE}")
        await conn.execute(
            f"REVOKE ALL ON SEQUENCE agent_turn_audit_id_seq FROM {ROLE}"
        )
        await conn.execute(f"DROP ROLE IF EXISTS {ROLE}")


async def _probe_pool() -> asyncpg.Pool:
    """A pool whose connections run as the non-superuser role, not as postgres.

    ``reset`` is a no-op instead of asyncpg's default ``RESET ALL``, which
    would otherwise wipe ``app.current_tenant`` on release and hide a SET
    SESSION leak. That is what makes the is_local=true flag observable: with
    the reset disabled, a session-scoped setting survives a checkout exactly
    the way it would on a pool that recycles connections without resetting
    custom GUCs.
    """

    async def _init(conn: asyncpg.Connection) -> None:
        await conn.execute(f"SET ROLE {ROLE}")

    async def _no_reset(conn: asyncpg.Connection) -> None:
        pass

    return await asyncpg.create_pool(
        os.environ["POSTGRES_URL"], min_size=1, max_size=1, init=_init,
        reset=_no_reset,
    )


async def test_record_turn_leaves_a_row_as_a_non_superuser(db_pool, monkeypatch):
    """With the tenant context set, record_turn's INSERT passes RLS for a
    least-privilege role. Removing the set_config makes this red: record_turn
    swallows the write failure by design, so the observable is the missing row.
    """
    await _create_probe_role(db_pool)
    pool = await _probe_pool()
    try:
        async def get_pool():
            return pool

        monkeypatch.setattr("app.audit.repository.get_pool", get_pool)

        await record_turn(_ctx(), "hola", _result(), 123, "t-rls")

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tenant_id, outcome FROM agent_turn_audit "
                "WHERE trace_id = 't-rls'"
            )
        assert row is not None
        assert row["tenant_id"] == "tenant_a"
        assert row["outcome"] == "ok"
    finally:
        await pool.close()
        await _drop_probe_role(db_pool)


async def test_quota_reads_see_rows_as_a_non_superuser(db_pool, monkeypatch):
    """check_and_count sets the tenant context before its SELECTs; without it a
    least-privilege role's counts silently see zero rows and the cap never trips.
    """
    await _create_probe_role(db_pool)
    async with db_pool.acquire() as conn:
        for i in range(2):
            await conn.execute(
                """INSERT INTO agent_turn_audit
                   (trace_id, tenant_id, user_id, channel, channel_user_id,
                    outcome, created_at)
                   VALUES ($1,'tenant_a','u','telegram','42','ok', now())""",
                f"seed-rls-{i}",
            )

    pool = await _probe_pool()
    try:
        async def get_pool():
            return pool

        monkeypatch.setattr("app.agent.quota.get_pool", get_pool)
        monkeypatch.setenv("MAX_TURNS_PER_ACCOUNT_HOUR", "2")
        get_settings.cache_clear()

        assert await check_and_count("tenant_a", "telegram", "42") == "account_hourly"
    finally:
        get_settings.cache_clear()
        await pool.close()
        await _drop_probe_role(db_pool)


async def test_tenant_context_does_not_survive_the_transaction(db_pool, monkeypatch):
    """set_config(..., is_local=true) is SET LOCAL: the context dies with the
    transaction, so a pool connection recycled to another tenant inherits
    nothing. Setting is_local to false (SET SESSION) makes this red — the
    setting survives the transaction and this same connection still sees
    tenant_a's row after it commits.
    """
    await _create_probe_role(db_pool)
    pool = await _probe_pool()
    try:
        async def get_pool():
            return pool

        monkeypatch.setattr("app.audit.repository.get_pool", get_pool)

        # One turn for tenant_a sets the context locally and inserts its row.
        await record_turn(_ctx("tenant_a", "t-leak"), "hola", _result(), 10, "t-leak")

        # Same pooled connection (min_size=1) after the transaction: the context
        # is gone, so the RLS USING clause matches no rows — tenant_a's row is
        # invisible to a recycled connection that has no tenant context.
        async with pool.acquire() as conn:
            visible = await conn.fetchval("SELECT count(*) FROM agent_turn_audit")
        assert visible == 0
    finally:
        await pool.close()
        await _drop_probe_role(db_pool)
