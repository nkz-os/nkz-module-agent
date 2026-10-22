"""Tests for the 24h conversation memory (app.agent.memory).

Each test is built around the mutation that turns it red — the point is not
that a happy path works, but that the isolation, retention, bounding and
noise-filtering properties are load-bearing and would be caught if removed.
"""

import logging

import pytest

from app.agent import memory
from app.config import get_settings
from app.domain.session import SessionContext
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


def _ctx(tenant_id="tenant_a", channel_user_id="42") -> SessionContext:
    return SessionContext(
        tenant_id=tenant_id,
        user_id="user_a",
        roles=("Farmer",),
        channel="telegram",
        channel_user_id=channel_user_id,
        trace_id="t-mem",
    )


async def test_save_then_load_round_trip(db_pool):
    thread = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "¿en qué te ayudo?"},
    ]
    await memory.save(_ctx(), thread, 10_000)
    assert await memory.load(_ctx(), budget_chars=10_000) == thread


async def test_expired_thread_loads_empty(db_pool):
    """Mutation: dropping the TTL predicate would resurrect a day-old thread."""
    await memory.save(_ctx(), [{"role": "user", "content": "hola"}], 10_000)
    ttl = get_settings().memory_ttl_hours

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_conversation_state "
            "SET updated_at = now() - ($1 || ' hours')::interval "
            "WHERE tenant_id = $2 AND channel = $3 AND channel_user_id = $4",
            str(ttl + 1), "tenant_a", "telegram", "42",
        )

    assert await memory.load(_ctx(), budget_chars=10_000) == []


async def test_trim_keeps_newest_messages_with_marker(db_pool):
    """The most recent messages survive a cut, and the gap is marked.

    Mutation: removing the trim would return the whole 61-char thread for a
    25-char budget — red by excess, not by length alone. The content assertion
    proves the kept messages are the NEWEST (tercero / respuesta-tres), not
    merely that *some* suffix of the right size was kept.
    """
    thread = [
        {"role": "user", "content": "primero"},
        {"role": "assistant", "content": "respuesta-uno"},
        {"role": "user", "content": "segundo"},
        {"role": "assistant", "content": "respuesta-dos"},
        {"role": "user", "content": "tercero"},
        {"role": "assistant", "content": "respuesta-tres"},
    ]
    await memory.save(_ctx(), thread, 10_000)

    loaded = await memory.load(_ctx(), budget_chars=25)

    assert sum(len(str(m.get("content", ""))) for m in loaded) <= 25
    assert [m["content"] for m in loaded] == ["…", "tercero", "respuesta-tres"]
    assert loaded[0]["role"] == "system"


async def test_load_is_isolated_by_tenant(db_pool):
    """The important one: same channel_user_id on another tenant sees nothing.

    Mutation: dropping tenant_id from the load WHERE clause leaks tenant A's
    thread to tenant B when both happen to share channel_user_id '42'.
    """
    ctx_a = _ctx(tenant_id="tenant_a")
    ctx_b = _ctx(tenant_id="tenant_b")

    await memory.save(ctx_a, [
        {"role": "user", "content": "secreto de A"},
        {"role": "assistant", "content": "respuesta de A"},
    ], 10_000)

    assert await memory.load(ctx_b, budget_chars=10_000) == []


async def test_tool_messages_are_not_persisted(db_pool):
    """Only the user message and the final assistant answer survive a save.

    Mutation: persisting the full turn list would carry role:tool results and
    the intermediate assistant tool_calls into the next turn's context.
    """
    full_turn = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "consulta"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "lookup", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "resultado intermedio"},
        {"role": "assistant", "content": "respuesta final"},
    ]
    await memory.save(_ctx(), full_turn, 10_000)

    assert await memory.load(_ctx(), budget_chars=10_000) == [
        {"role": "user", "content": "consulta"},
        {"role": "assistant", "content": "respuesta final"},
    ]


async def test_purge_removes_expired_rows(db_pool, monkeypatch):
    """Expired rows disappear on the opportunistic hook.

    The cadence is forced to every save so the hook fires deterministically;
    without the purge the expired row would survive indefinitely (storage
    reclamation only — load already treats it as empty).
    """
    monkeypatch.setattr(memory, "_PURGE_EVERY_N_SAVES", 1)
    monkeypatch.setattr(memory, "_save_counter", 0)

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_conversation_state "
            "(tenant_id, channel, channel_user_id, messages, updated_at) "
            "VALUES ('old', 'telegram', '99', '[]'::jsonb, now() - interval '48 hours')"
        )

    await memory.save(_ctx(), [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ], 10_000)

    async with db_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM agent_conversation_state WHERE tenant_id = 'old'"
        )
    assert n == 0


async def test_purge_failure_does_not_break_save(db_pool, monkeypatch, caplog):
    """A purge that fails must never lose the turn (or the just-saved thread).

    The dedupe DELETE is the opportunistic extra; when it raises, the memory
    row is already committed and save still returns — only a WARNING is
    emitted. Mutation: letting purge_expired's failure propagate would turn a
    storage-reclamation failure into a lost conversation save.
    """
    monkeypatch.setattr(memory, "_PURGE_EVERY_N_SAVES", 1)
    monkeypatch.setattr(memory, "_save_counter", 0)

    async def broken_purge():
        raise RuntimeError("db gone")

    monkeypatch.setattr(memory, "purge_expired", broken_purge)
    caplog.set_level(logging.WARNING)

    await memory.save(_ctx(), [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ], 10_000)  # must not raise

    assert await memory.load(_ctx(), budget_chars=10_000) == [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]
    assert any("dedupe_purge_failed" in r.message for r in caplog.records)


async def test_memory_purge_failure_does_not_rollback_the_save(db_pool, monkeypatch, caplog):
    """The memory DELETE runs on its own transaction AFTER the upsert commit:
    a failing purge (here: a TTL that cannot cast to interval) must leave the
    just-saved thread intact. Mutation: putting the DELETE back inside the
    save transaction rolls back the INSERT and this goes red."""
    caplog.set_level(logging.WARNING)

    async def broken_purge():
        raise RuntimeError("purge path gone")

    with monkeypatch.context() as m:
        m.setattr(memory, "_PURGE_EVERY_N_SAVES", 1)
        m.setattr(memory, "_save_counter", 0)
        m.setattr(memory, "_purge_expired_rows", broken_purge)
        await memory.save(_ctx(), [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ], 10_000)  # must not raise

    assert await memory.load(_ctx(), budget_chars=10_000) == [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]
    assert any("memory_purge_failed" in r.message for r in caplog.records)


async def test_thread_grows_across_turns_and_trims_in_place(db_pool):
    """The memory is a ROLLING thread: saves append to what is stored, and a
    tight budget trims the STORED thread (the trim is live in the real path,
    not only at load). Mutation: replace-semantics (merged = essential only)
    leaves just the last exchange and the first assert goes red."""
    await memory.save(_ctx(), [
        {"role": "user", "content": "turno uno"},
        {"role": "assistant", "content": "respuesta uno"},
    ], 10_000)
    await memory.save(_ctx(), [
        {"role": "user", "content": "turno dos"},
        {"role": "assistant", "content": "respuesta dos"},
    ], 10_000)
    loaded = await memory.load(_ctx(), budget_chars=10_000)
    assert [m["content"] for m in loaded] == [
        "turno uno", "respuesta uno", "turno dos", "respuesta dos",
    ]

    await memory.save(_ctx(), [
        {"role": "user", "content": "turno tres"},
        {"role": "assistant", "content": "respuesta tres"},
    ], 60)  # fits the newest exchange, drops the oldest
    contents = [m["content"] for m in await memory.load(_ctx(), budget_chars=10_000)]
    assert "turno tres" in contents and "respuesta tres" in contents
    assert "turno uno" not in contents
