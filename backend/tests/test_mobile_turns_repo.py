import pytest

from app.mobile import turns
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def _make(key="k1", tenant="tenant_a", user="u1"):
    return await turns.create_turn(tenant, user, key, "hello", None, "es", "trace-1")


async def test_create_twice_same_key_is_idempotent(db_pool):
    first, created1 = await _make()
    second, created2 = await _make()
    assert created1 is True and created2 is False
    assert first.turn_id == second.turn_id
    assert first.state == "queued"


async def test_same_key_different_user_is_a_different_turn(db_pool):
    a, _ = await _make(user="u1")
    b, created = await _make(user="u2")
    assert created is True and a.turn_id != b.turn_id


async def test_get_turn_is_scoped_to_tenant_and_user(db_pool):
    t, _ = await _make()
    assert (await turns.get_turn("tenant_a", "u1", t.turn_id)).turn_id == t.turn_id
    assert await turns.get_turn("tenant_a", "u2", t.turn_id) is None
    assert await turns.get_turn("tenant_b", "u1", t.turn_id) is None


async def test_has_open_turn_until_done(db_pool):
    assert await turns.has_open_turn("tenant_a", "u1") is False
    t, _ = await _make()
    assert await turns.has_open_turn("tenant_a", "u1") is True
    await turns.mark_running(t.turn_id)
    assert await turns.has_open_turn("tenant_a", "u1") is True
    await turns.mark_done(t.turn_id, "reply", [{"source": "s"}], "ok")
    assert await turns.has_open_turn("tenant_a", "u1") is False
    done = await turns.get_turn("tenant_a", "u1", t.turn_id)
    assert done.state == "done" and done.reply_text == "reply"
    assert done.citations == [{"source": "s"}] and done.outcome == "ok"
    assert done.started_at is not None and done.finished_at is not None


async def test_mark_failed(db_pool):
    t, _ = await _make()
    await turns.mark_failed(t.turn_id, "error", None)
    row = await turns.get_turn("tenant_a", "u1", t.turn_id)
    assert row.state == "failed" and row.error_code == "error"
    assert row.finished_at is not None


async def test_list_history_newest_first_and_limited(db_pool):
    for i in range(3):
        await _make(key=f"k{i}")
    rows = await turns.list_history("tenant_a", "u1", 2)
    assert [r.client_turn_id for r in rows] == ["k2", "k1"]
    assert await turns.list_history("tenant_a", "u2", 5) == []


async def test_expire_orphans_fails_running_rows(db_pool):
    t, _ = await _make()
    await turns.mark_running(t.turn_id)
    assert await turns.expire_orphans(0) == 1
    row = await turns.get_turn("tenant_a", "u1", t.turn_id)
    assert row.state == "failed" and row.error_code == "interrupted"
    assert row.finished_at is not None
    assert await turns.expire_orphans(0) == 0


async def test_mark_running_returns_true_only_from_queued(db_pool):
    t, _ = await _make("rt1")
    assert await turns.mark_running(t.turn_id) is True
    assert await turns.mark_running(t.turn_id) is False


async def test_terminal_state_is_never_overwritten(db_pool):
    t, _ = await _make("rt2")
    await turns.mark_failed(t.turn_id, "interrupted", None)
    await turns.mark_done(t.turn_id, "late", [], "ok")
    got = await turns.get_turn(t.tenant_id, t.user_id, t.turn_id)
    assert got.state == "failed" and got.reply_text is None
    await turns.mark_failed(t.turn_id, "error", None)
    got = await turns.get_turn(t.tenant_id, t.user_id, t.turn_id)
    assert got.error_code == "interrupted"


async def test_get_by_client_turn_id_is_scoped(db_pool):
    t, _ = await _make("cid1")
    got = await turns.get_by_client_turn_id("tenant_a", "u1", "cid1")
    assert got is not None and got.turn_id == t.turn_id
    assert await turns.get_by_client_turn_id("tenant_a", "u2", "cid1") is None
    assert await turns.get_by_client_turn_id("tenant_b", "u1", "cid1") is None


async def test_expire_orphans_for_user_is_scoped(db_pool):
    mine, _ = await _make("eu1", user="u1")
    other, _ = await _make("eu2", user="u2")
    assert await turns.expire_orphans_for_user("tenant_a", "u1", 0) == 1
    assert (await turns.get_turn("tenant_a", "u1", mine.turn_id)).error_code == "interrupted"
    assert (await turns.get_turn("tenant_a", "u2", other.turn_id)).state == "queued"
    assert await turns.has_open_turn("tenant_a", "u1") is False


async def test_list_history_presents_stale_open_rows_as_interrupted(db_pool):
    from app.db import get_pool

    t, _ = await _make("st1")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_mobile_turns SET created_at = now() - interval '1 hour' "
            "WHERE turn_id = $1::uuid", t.turn_id,
        )
    fresh, _ = await _make("st2")
    rows = {r.client_turn_id: r for r in await turns.list_history("tenant_a", "u1", 10)}
    assert rows["st1"].state == "failed" and rows["st1"].error_code == "interrupted"
    assert rows["st2"].state == "queued"


async def test_list_history_only_within_memory_ttl(db_pool):
    from app.config import get_settings
    from app.db import get_pool

    old, _ = await _make("ttl1")
    new, _ = await _make("ttl2")
    pool = await get_pool()
    hours = get_settings().memory_ttl_hours
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_mobile_turns SET state = 'done', "
            "created_at = now() - make_interval(hours => $2) WHERE turn_id = $1::uuid",
            old.turn_id, hours + 1,
        )
    rows = await turns.list_history("tenant_a", "u1", 10)
    assert [r.client_turn_id for r in rows] == ["ttl2"]


async def test_skipped_terminal_write_is_logged(db_pool, caplog):
    import logging

    t, _ = await _make("sk1")
    await turns.mark_failed(t.turn_id, "interrupted", None)
    with caplog.at_level(logging.WARNING, logger="app.mobile.turns"):
        await turns.mark_done(t.turn_id, "late answer", [], "ok")
        await turns.mark_failed(t.turn_id, "error", None)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("mobile_turn_terminal_overwrite_skipped" in m for m in msgs) == 2
    assert all(t.turn_id in m and "state=failed" in m for m in msgs)
