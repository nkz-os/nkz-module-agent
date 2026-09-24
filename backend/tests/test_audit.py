import json

import pytest

from app.agent.loop import TurnResult
from app.audit.repository import record_turn
from app.domain.session import SessionContext
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


def _ctx() -> SessionContext:
    return SessionContext(
        tenant_id="tenant_a", user_id="user_a", roles=("Farmer",),
        channel="telegram", channel_user_id="42", trace_id="t-1",
    )


def _result(outcome="ok", tool_calls=()) -> TurnResult:
    return TurnResult(text="hola", outcome=outcome, model="m",
                      tool_calls=tool_calls, tokens_prompt=10, tokens_completion=5)


async def test_a_successful_turn_is_recorded(db_pool):
    await record_turn(_ctx(), "hola", _result(), 123, "t-1")
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT * FROM agent_turn_audit WHERE trace_id='t-1'")
    assert row["tenant_id"] == "tenant_a"
    assert row["outcome"] == "ok"
    assert row["latency_ms"] == 123
    assert row["tokens_prompt"] == 10
    assert row["tokens_completion"] == 5
    assert row["model"] == "m"
    assert row["reply_text"] == "hola"


async def test_a_failed_turn_is_also_recorded(db_pool):
    """The failed turn is the one somebody will ask about later.

    An audit trail that only records successes answers the question nobody has.
    """
    await record_turn(_ctx(), "hola", _result(outcome="error"), 50, "t-2")
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT outcome FROM agent_turn_audit WHERE trace_id='t-2'")
    assert row["outcome"] == "error"


async def test_recording_never_raises_into_the_turn(db_pool, monkeypatch):
    """Auditing must not be able to break answering.

    If the audit write fails, the farmer still gets their reply -- losing the
    record is bad, losing the conversation is worse.
    """
    async def broken():
        raise RuntimeError("db gone")

    monkeypatch.setattr("app.audit.repository.get_pool", broken)
    await record_turn(_ctx(), "hola", _result(), 10, "t-3")  # must not raise


async def test_rows_are_scoped_by_tenant(db_pool):
    await record_turn(_ctx(), "hola", _result(), 10, "t-4")
    async with db_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM agent_turn_audit WHERE tenant_id='tenant_b'")
    assert n == 0


async def test_refused_tool_calls_survive_the_jsonb_round_trip(db_pool):
    """Close the loop with app.agent.loop: the tool call the model attempted
    (and was refused, empty registry) must come back out of the jsonb column
    exactly as the loop reported it."""
    call = {"id": "1", "function": {"name": "get_parcels", "arguments": "{}"}}
    await record_turn(_ctx(), "como va mi parcela",
                      _result(outcome="refused", tool_calls=(call,)), 40, "t-5")
    async with db_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT tool_calls, outcome FROM agent_turn_audit WHERE trace_id='t-5'")
    assert row["outcome"] == "refused"
    # asyncpg hands jsonb back as text unless a codec is registered on the
    # connection, so the round trip ends with a json.loads here.
    assert json.loads(row["tool_calls"]) == [call]
