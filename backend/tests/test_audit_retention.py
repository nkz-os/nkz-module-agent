import logging
import os

import asyncpg
import pytest

from app.agent.loop import TurnResult
from app.agent.quota import check_and_count
from app.audit import repository
from app.audit.repository import record_turn, scrub_expired_texts
from app.config import get_settings
from app.domain.session import SessionContext
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


def _ctx(tenant: str = "tenant_a") -> SessionContext:
    return SessionContext(
        tenant_id=tenant, user_id="user_a", roles=("Farmer",),
        channel="telegram", channel_user_id="42", trace_id="t",
    )


def _result() -> TurnResult:
    return TurnResult(text="reply", outcome="ok", model="m",
                      tool_calls=({"name": "x"},), tokens_prompt=10,
                      tokens_completion=5)


_REAL_SHOULD_SCRUB = repository._should_scrub


@pytest.fixture(autouse=True)
def _never_scrub_on_cadence(monkeypatch):
    """Seeding goes through record_turn; keep the opportunistic hook off so
    only the tests that exercise it see it."""
    repository._last_scrub.clear()
    monkeypatch.setattr(repository, "_should_scrub", lambda tenant_id: False)
    yield
    repository._last_scrub.clear()


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed(db_pool):
    """a-old (91 d), a-new, b-old (91 d)."""
    await record_turn(_ctx("tenant_a"), "in a-old", _result(), 11, "a-old")
    await record_turn(_ctx("tenant_a"), "in a-new", _result(), 12, "a-new")
    await record_turn(_ctx("tenant_b"), "in b-old", _result(), 13, "b-old")
    async with db_pool.acquire() as c:
        await c.execute(
            "UPDATE agent_turn_audit SET created_at = now() - interval '91 days' "
            "WHERE trace_id IN ('a-old', 'b-old')"
        )
    # Seeding goes through record_turn, which under the real cadence marks the
    # tenant as just scrubbed; the tests start from "no scrub yet".
    repository._last_scrub.clear()


async def _row(db_pool, trace_id):
    async with db_pool.acquire() as c:
        return await c.fetchrow(
            "SELECT * FROM agent_turn_audit WHERE trace_id = $1", trace_id)


async def test_scrub_nulls_only_expired_texts_of_that_tenant(db_pool):
    await _seed(db_pool)
    before = await _row(db_pool, "a-old")

    assert await scrub_expired_texts("tenant_a") == 1

    old = await _row(db_pool, "a-old")
    assert old["inbound_text"] is None and old["reply_text"] is None
    for col in ("outcome", "tokens_prompt", "tokens_completion", "latency_ms",
                "model", "created_at", "tool_calls", "tenant_id", "user_id",
                "channel", "channel_user_id"):
        assert old[col] == before[col], col

    new = await _row(db_pool, "a-new")
    assert new["inbound_text"] == "in a-new" and new["reply_text"] == "reply"
    other = await _row(db_pool, "b-old")
    assert other["inbound_text"] == "in b-old" and other["reply_text"] == "reply"


async def test_scrub_is_idempotent(db_pool):
    await _seed(db_pool)
    assert await scrub_expired_texts("tenant_a") == 1
    assert await scrub_expired_texts("tenant_a") == 0


async def test_retention_window_is_configurable(db_pool, monkeypatch):
    await _seed(db_pool)
    monkeypatch.setenv("AUDIT_TEXT_RETENTION_DAYS", "1000")
    get_settings.cache_clear()
    assert await scrub_expired_texts("tenant_a") == 0
    assert (await _row(db_pool, "a-old"))["inbound_text"] == "in a-old"


@pytest.fixture
def real_cadence(monkeypatch):
    """The real per-tenant cadence, with a controllable monotonic clock."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(repository, "_should_scrub", _REAL_SHOULD_SCRUB)
    monkeypatch.setattr(repository, "_monotonic", lambda: clock["t"])
    return clock


async def test_first_turn_of_a_tenant_scrubs(db_pool, real_cadence):
    await _seed(db_pool)
    await record_turn(_ctx("tenant_a"), "in a-3", _result(), 10, "a-3")
    assert (await _row(db_pool, "a-old"))["inbound_text"] is None
    assert (await _row(db_pool, "a-3"))["inbound_text"] == "in a-3"
    # Only that tenant: tenant_b has not had a turn yet.
    assert (await _row(db_pool, "b-old"))["inbound_text"] == "in b-old"


async def test_second_turn_within_24h_does_not_scrub(db_pool, real_cadence):
    await record_turn(_ctx("tenant_a"), "in first", _result(), 10, "first")
    await _seed(db_pool)  # expired rows appear after the first scrub
    repository._last_scrub["tenant_a"] = 1000.0  # as the first turn left it
    real_cadence["t"] += 3600
    await record_turn(_ctx("tenant_a"), "in a-3", _result(), 10, "a-3")
    assert (await _row(db_pool, "a-old"))["inbound_text"] == "in a-old"


async def test_turn_after_24h_scrubs_again(db_pool, real_cadence):
    await record_turn(_ctx("tenant_a"), "in first", _result(), 10, "first")
    await _seed(db_pool)
    repository._last_scrub["tenant_a"] = 1000.0  # as the first turn left it
    real_cadence["t"] += 24 * 3600 + 1
    await record_turn(_ctx("tenant_a"), "in a-3", _result(), 10, "a-3")
    assert (await _row(db_pool, "a-old"))["inbound_text"] is None


async def test_cadence_is_per_tenant_not_global(db_pool, real_cadence):
    """A busy tenant's turns never use up a quiet tenant's scrub."""
    await _seed(db_pool)
    for i in range(25):
        await record_turn(_ctx("tenant_a"), f"in {i}", _result(), 1, f"busy-{i}")
    assert (await _row(db_pool, "b-old"))["inbound_text"] == "in b-old"
    await record_turn(_ctx("tenant_b"), "in b-new", _result(), 1, "b-new")
    assert (await _row(db_pool, "b-old"))["inbound_text"] is None


async def test_failed_scrub_is_retried_on_the_next_turn(db_pool, real_cadence, monkeypatch):
    real = repository.scrub_expired_texts
    calls = {"n": 0}

    async def flaky(tenant_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return await real(tenant_id)

    await _seed(db_pool)
    monkeypatch.setattr(repository, "scrub_expired_texts", flaky)
    await record_turn(_ctx("tenant_a"), "in x", _result(), 1, "x")
    assert (await _row(db_pool, "a-old"))["inbound_text"] == "in a-old"
    await record_turn(_ctx("tenant_a"), "in y", _result(), 1, "y")
    assert (await _row(db_pool, "a-old"))["inbound_text"] is None


async def test_scrub_failure_never_loses_the_audit_row(db_pool, monkeypatch, caplog):
    async def boom(tenant_id):
        raise RuntimeError("scrub exploded")

    monkeypatch.setattr(repository, "_should_scrub", lambda tenant_id: True)
    monkeypatch.setattr(repository, "scrub_expired_texts", boom)
    with caplog.at_level(logging.ERROR):
        await record_turn(_ctx("tenant_a"), "in a-3", _result(), 10, "a-3")  # no raise
    assert (await _row(db_pool, "a-3")) is not None
    assert "audit_scrub_failed" in caplog.text


async def test_scrub_does_not_change_quota_verdict(db_pool, monkeypatch):
    """The quota counts rows by created_at; nulling text must not alter it.

    Three recent turns plus one 91-day-old row: a cap of 3 trips on the recent
    ones only, a cap of 4 does not (the old row is outside the day window), and
    scrubbing the old row's text changes neither verdict nor any row count.
    """
    await _seed(db_pool)  # a-old (91 d), a-new, b-old (91 d)
    await record_turn(_ctx("tenant_a"), "in a-2", _result(), 14, "a-2")
    await record_turn(_ctx("tenant_a"), "in a-3", _result(), 15, "a-3")

    async def verdicts():
        out = []
        for limit in ("3", "4"):
            monkeypatch.setenv("MAX_TURNS_PER_TENANT_DAY", limit)
            get_settings.cache_clear()
            out.append(await check_and_count("tenant_a", "telegram", "42"))
        return out

    before = await verdicts()
    assert before == ["tenant_daily", None]
    assert await scrub_expired_texts("tenant_a") == 1
    assert await verdicts() == before
    async with db_pool.acquire() as c:
        assert await c.fetchval(
            "SELECT count(*) FROM agent_turn_audit WHERE tenant_id='tenant_a'") == 4


@pytest.fixture
async def role_pool():
    """Pool whose connections run as the module's real role (NOSUPERUSER,
    NOBYPASSRLS), so FORCE row-level security applies as in production."""
    async def _as_module_role(conn):
        await conn.execute("SET ROLE nkz_module_agent")

    pool = await asyncpg.create_pool(
        os.environ["POSTGRES_URL"],
        server_settings={"search_path": "agent_module,public"},
        init=_as_module_role,
    )
    yield pool
    await pool.close()


async def test_scrub_under_module_role_relies_on_rls_context(
    db_pool, role_pool, monkeypatch
):
    """Production path: without set_config the role sees no rows and the scrub
    silently does nothing."""
    await _seed(db_pool)

    async def _get_pool():
        return role_pool

    monkeypatch.setattr(repository, "get_pool", _get_pool)
    assert await scrub_expired_texts("tenant_a") == 1

    old = await _row(db_pool, "a-old")
    assert old["inbound_text"] is None and old["reply_text"] is None
    assert (await _row(db_pool, "a-new"))["inbound_text"] == "in a-new"
    assert (await _row(db_pool, "b-old"))["inbound_text"] == "in b-old"


async def test_rls_alone_isolates_tenants_under_module_role(db_pool, role_pool):
    """A tenant-less UPDATE under tenant_a's context must not reach tenant_b."""
    await _seed(db_pool)
    async with role_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant', $1, true)", "tenant_a")
        touched = await conn.execute(
            "UPDATE agent_turn_audit SET reply_text = NULL "
            "WHERE created_at < now() - interval '90 days'"
        )
    assert touched == "UPDATE 1"
    assert (await _row(db_pool, "a-old"))["reply_text"] is None
    assert (await _row(db_pool, "b-old"))["reply_text"] == "reply"
