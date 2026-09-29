"""Schema-ownership migration tests, against a real PostgreSQL.

The module now owns its schema: these prove the migration files do the three
things a shell harness would otherwise check by hand — adopt the pre-existing
public tables into ``agent_module``, create them there on a virgin database,
and be safe to re-run — plus the two guards that pin behaviour that is easy to
lose silently (the pool's ``search_path`` and the role's grants).
"""

from __future__ import annotations

import inspect
import os

import app.db
import asyncpg
import pytest
from app.config import get_settings
from app.db import close_pool, get_pool

from tests.conftest import MIGRATION_000, MIGRATION_001, MIGRATION_002, requires_db

DSN = os.environ["POSTGRES_URL"]

# The historical 101 DDL, unqualified, exactly as it lived in ``public`` before
# this change. Used to reproduce the production "adopt" state: tables already
# present in public that 001 must move rather than recreate.
OLD_101_DDL = """
CREATE TABLE IF NOT EXISTS agent_channel_links (
    id              BIGSERIAL PRIMARY KEY,
    channel         TEXT        NOT NULL,
    channel_user_id TEXT        NOT NULL,
    tenant_id       TEXT        NOT NULL,
    user_id         TEXT        NOT NULL,
    roles           TEXT[]      NOT NULL DEFAULT '{}',
    status          TEXT        NOT NULL DEFAULT 'active',
    linked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ,
    last_seen_at    TIMESTAMPTZ,
    CONSTRAINT agent_channel_links_status_ck
        CHECK (status IN ('active', 'revoked'))
);

CREATE UNIQUE INDEX IF NOT EXISTS agent_channel_links_active_uq
    ON agent_channel_links (channel, channel_user_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS agent_channel_links_tenant_idx
    ON agent_channel_links (tenant_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS agent_link_tokens (
    token_hash  TEXT        PRIMARY KEY,
    tenant_id   TEXT        NOT NULL,
    user_id     TEXT        NOT NULL,
    roles       TEXT[]      NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agent_link_tokens_expiry_idx
    ON agent_link_tokens (expires_at);

CREATE TABLE IF NOT EXISTS agent_processed_updates (
    idempotency_key TEXT        PRIMARY KEY,
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_processed_updates_seen_idx
    ON agent_processed_updates (seen_at);
"""

_IDENTITY_TABLES = (
    "agent_channel_links",
    "agent_link_tokens",
    "agent_processed_updates",
)


async def _clean_slate(conn: asyncpg.Connection) -> None:
    """Drop every trace of the module schema so a test starts from scratch.

    Roles are cluster-level and left alone; only schemas/tables are reset.
    """
    await conn.execute("DROP SCHEMA IF EXISTS agent_module CASCADE")
    for t in _IDENTITY_TABLES + ("agent_turn_audit",):
        await conn.execute(f"DROP TABLE IF EXISTS public.{t} CASCADE")


async def _table_schemas(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT tablename, schemaname FROM pg_tables "
        f"WHERE tablename IN ({', '.join('$' + str(i + 1) for i in range(len(_IDENTITY_TABLES)))})",
        *_IDENTITY_TABLES,
    )
    return {r["tablename"]: r["schemaname"] for r in rows}


@requires_db
@pytest.mark.asyncio
async def test_001_adopts_existing_public_tables():
    """Production shape: tables already in public. 001 moves them, data intact."""
    conn = await asyncpg.connect(DSN)
    try:
        await _clean_slate(conn)
        await conn.execute(MIGRATION_000.read_text())
        await conn.execute(OLD_101_DDL)
        await conn.execute(
            "INSERT INTO agent_channel_links "
            "(channel, channel_user_id, tenant_id, user_id) "
            "VALUES ('telegram','42','tenant_a','user_a')"
        )
        await conn.execute(MIGRATION_001.read_text())

        assert await _table_schemas(conn) == {
            "agent_channel_links": "agent_module",
            "agent_link_tokens": "agent_module",
            "agent_processed_updates": "agent_module",
        }
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename IN "
            "('agent_channel_links','agent_link_tokens','agent_processed_updates')"
        ) == 0

        # Unqualified queries resolve via search_path, and the moved row is
        # still there.
        await conn.execute("SET search_path = agent_module, public")
        assert await conn.fetchval("SELECT count(*) FROM agent_channel_links") == 1
    finally:
        await conn.close()


@requires_db
@pytest.mark.asyncio
async def test_001_creates_tables_in_agent_module_on_clean_db():
    """Virgin shape: 001 creates the tables in agent_module, never in public."""
    conn = await asyncpg.connect(DSN)
    try:
        await _clean_slate(conn)
        await conn.execute(MIGRATION_000.read_text())
        await conn.execute(MIGRATION_001.read_text())

        assert await _table_schemas(conn) == {
            "agent_channel_links": "agent_module",
            "agent_link_tokens": "agent_module",
            "agent_processed_updates": "agent_module",
        }
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename IN "
            "('agent_channel_links','agent_link_tokens','agent_processed_updates')"
        ) == 0
    finally:
        await conn.close()


@requires_db
@pytest.mark.asyncio
async def test_001_and_002_are_idempotent():
    conn = await asyncpg.connect(DSN)
    try:
        await _clean_slate(conn)
        await conn.execute(MIGRATION_000.read_text())
        for _ in range(2):
            await conn.execute(MIGRATION_001.read_text())
            await conn.execute(MIGRATION_002.read_text())

        assert await conn.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'agent_module' "
            "AND tablename IN "
            "('agent_channel_links','agent_link_tokens','agent_processed_updates',"
            "'agent_turn_audit')"
        ) == 4
    finally:
        await conn.close()


@requires_db
@pytest.mark.asyncio
async def test_app_pool_pins_search_path():
    """Pin by config, not by "a query happens to work".

    The literal must be present in app.db's pool construction AND actually take
    effect on the connection. Removing the server_settings kwarg makes the
    source assertion red; the SHOW makes it red even if the literal were moved
    somewhere ineffective.
    """
    assert '"search_path": "agent_module,public"' in inspect.getsource(app.db)

    get_settings.cache_clear()
    await close_pool()
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            search_path = await conn.fetchval("SHOW search_path")
            assert search_path.replace(" ", "") == "agent_module,public"
    finally:
        await close_pool()


@requires_db
@pytest.mark.asyncio
async def test_000_is_idempotent_and_creates_login_role_without_password():
    sql = MIGRATION_000.read_text()
    # Never a password in a public repo. Check the executable SQL (strip the
    # `--` comment lines, which may legitimately mention the word).
    executable = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    assert "PASSWORD" not in executable.upper()

    conn = await asyncpg.connect(DSN)
    try:
        # Roles are cluster-level and survive the DB reset, so a prior run (or
        # the fixture) may have left one — possibly with an out-of-band password
        # in this environment. Drop it (and the schema that holds its grants) so
        # we test 000's own CREATE ROLE deterministically.
        await _clean_slate(conn)
        await conn.execute("DROP ROLE IF EXISTS agent_module")
        await conn.execute(sql)
        await conn.execute(sql)  # idempotent
        row = await conn.fetchrow(
            "SELECT rolcanlogin, (passwd IS NULL) AS no_password "
            "FROM pg_roles r LEFT JOIN pg_shadow s ON s.usename = r.rolname "
            "WHERE r.rolname = 'agent_module'"
        )
        assert row is not None
        assert row["rolcanlogin"] is True
        assert row["no_password"] is True
    finally:
        await conn.close()


@requires_db
@pytest.mark.asyncio
async def test_agent_module_role_can_write_its_own_audit_rows():
    """The grants shipped by 001/002 are what make the least-privilege role work.

    With the tenant context set, the INSERT satisfies RLS; so a failure here is
    a missing grant (permission), never a row-level-security error. Removing the
    sequence grant in 002, or USAGE on the schema in 001, turns this red.
    """
    conn = await asyncpg.connect(DSN)
    try:
        await _clean_slate(conn)
        await conn.execute(MIGRATION_000.read_text())
        await conn.execute(MIGRATION_001.read_text())
        await conn.execute(MIGRATION_002.read_text())

        await conn.execute("SET ROLE agent_module")
        await conn.execute("SET search_path = agent_module, public")
        await conn.execute("SET app.current_tenant = 'tenant_a'")
        await conn.execute(
            "INSERT INTO agent_turn_audit "
            "(trace_id, tenant_id, user_id, channel, channel_user_id, outcome) "
            "VALUES ('t-role','tenant_a','u','telegram','42','ok')"
        )
        assert await conn.fetchval("SELECT count(*) FROM agent_turn_audit") == 1
    finally:
        await conn.execute("RESET ROLE")
        await conn.close()


@requires_db
@pytest.mark.asyncio
async def test_audit_sequence_missing_grant_fails_with_permission_not_rls():
    """Distinguish the two failure modes the mutations can produce.

    Without the sequence grant, the INSERT fails on permissions before the
    policy is consulted — a pass for the wrong reason if we only matched
    "row-level security". Assert the permission error text instead.
    """
    conn = await asyncpg.connect(DSN)
    try:
        await _clean_slate(conn)
        await conn.execute(MIGRATION_000.read_text())
        await conn.execute(MIGRATION_001.read_text())
        await conn.execute(MIGRATION_002.read_text())
        await conn.execute(
            "REVOKE USAGE, SELECT ON SEQUENCE "
            "agent_module.agent_turn_audit_id_seq FROM agent_module"
        )

        await conn.execute("SET ROLE agent_module")
        await conn.execute("SET search_path = agent_module, public")
        await conn.execute("SET app.current_tenant = 'tenant_a'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as exc:
            await conn.execute(
                "INSERT INTO agent_turn_audit "
                "(trace_id, tenant_id, user_id, channel, channel_user_id, outcome) "
                "VALUES ('t','tenant_a','u','telegram','42','ok')"
            )
        assert "sequence" in str(exc.value)
        assert "row-level security" not in str(exc.value)
    finally:
        await conn.execute("RESET ROLE")
        await conn.close()


@pytest.mark.asyncio
async def test_identity_table_grants_work_for_agent_module_role():
    """The 001 CRUD grants are exercised with the role that will use them.

    The audit-table grants (002) are covered by the RLS tests; these three
    identity tables had no test running as ``agent_module`` — a missing or
    mistyped grant in 001 would otherwise surface only in production, after
    the DSN swap. Full lifecycle as the role: INSERT, SELECT, UPDATE, DELETE.
    """
    conn = await asyncpg.connect(DSN)
    try:
        await _clean_slate(conn)
        await conn.execute(MIGRATION_000.read_text())
        await conn.execute(MIGRATION_001.read_text())
        await conn.execute(MIGRATION_002.read_text())

        await conn.execute("SET ROLE agent_module")
        await conn.execute("SET search_path = agent_module, public")
        await conn.execute(
            "INSERT INTO agent_channel_links "
            "(tenant_id, user_id, channel, channel_user_id) "
            "VALUES ('tenant_a', 'u1', 'telegram', '42')"
        )
        seen = await conn.fetchval("SELECT count(*) FROM agent_channel_links")
        assert seen == 1
        await conn.execute(
            "UPDATE agent_channel_links SET channel_user_id = '43' "
            "WHERE tenant_id = 'tenant_a'"
        )
        await conn.execute("DELETE FROM agent_channel_links WHERE tenant_id = 'tenant_a'")
        seen = await conn.fetchval("SELECT count(*) FROM agent_channel_links")
        assert seen == 0
    finally:
        await conn.execute("RESET ROLE")
        await conn.close()
