import asyncio
import logging
from datetime import datetime, timezone

import pytest

from app import handlers
from app.agent.loop import TurnResult
from app.config import get_settings
from app.domain.messages import InboundMessage
from app.handlers import handle_message
from app.identity import repository as repo
from app.identity import service
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


def _msg(text: str, user: str = "42") -> InboundMessage:
    return InboundMessage(
        channel="telegram", channel_user_id=user, conversation_id=user, text=text,
        voice=None, idempotency_key=f"telegram:{user}",
        received_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def clear_settings_cache():
    """Clear get_settings()'s cache on teardown, even if the test fails.

    A trailing cache_clear() on a test's last line never runs once an
    earlier assertion raises, poisoning the cache for the rest of the
    session. Same fixture as test_identity_service.py — duplicated here
    because it is a local fixture there, not a shared conftest one.
    """
    yield
    from app.config import get_settings

    get_settings.cache_clear()


async def test_unlinked_user_is_told_to_link(db_pool):
    reply = await handle_message(_msg("hola", user="999"), trace_id="t-1")
    assert reply.text == handlers.NOT_LINKED


async def test_start_with_valid_token_links_the_account(db_pool):
    token, _ = await service.create_link_token("tenant_a", "user_a", ("Farmer",))

    reply = await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    assert reply.text == handlers.LINK_OK
    ctx = await service.resolve_session("telegram", "42", "t-2")
    assert ctx is not None and ctx.tenant_id == "tenant_a"


async def test_link_confirmation_discloses_it_is_automated(db_pool):
    """Platform requirement: the user must know they are talking to software."""
    token, _ = await service.create_link_token("tenant_a", "user_a", ())

    reply = await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    assert "automated" in reply.text.lower()
    assert reply.text == handlers.LINK_OK


async def test_start_with_bad_token_does_not_link(db_pool):
    reply = await handle_message(_msg("/start not-a-real-token"), trace_id="t-1")
    assert reply.text == handlers.LINK_FAILED
    assert await service.resolve_session("telegram", "42", "t-2") is None


async def test_unlink_revokes_the_link(db_pool):
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    reply = await handle_message(_msg("/unlink"), trace_id="t-2")

    assert reply.text == handlers.UNLINK_OK
    assert await service.resolve_session("telegram", "42", "t-3") is None


async def test_unlink_cannot_revoke_another_tenants_link(db_pool):
    """Security property: one tenant must never revoke another tenant's link.

    handle_message can never itself send a cross-tenant revoke — the tenant it
    passes to revoke_link always comes from the caller's own resolved session,
    never from user input. This probes the guarantee at the layer where a
    future refactor could actually break it: the repository call itself.
    """
    token_a, _ = await service.create_link_token("tenant_a", "user_a", ())
    token_b, _ = await service.create_link_token("tenant_b", "user_b", ())
    await handle_message(_msg(f"/start {token_a}", user="42"), trace_id="t-1")
    await handle_message(_msg(f"/start {token_b}", user="43"), trace_id="t-2")

    link_a = await repo.get_active_link("telegram", "42")
    assert link_a is not None

    revoked = await repo.revoke_link(link_a["id"], "tenant_b")

    assert revoked is False
    assert await repo.get_active_link("telegram", "42") is not None
    ctx = await service.resolve_session("telegram", "42", "t-3")
    assert ctx is not None and ctx.tenant_id == "tenant_a"


# --- Task 9: the final branch runs the agent ---------------------------------


async def test_linked_user_gets_an_agent_reply(db_pool, monkeypatch):
    async def fake_turn(text, budget):
        return TurnResult("respuesta del agente", "ok", "m", (), 1, 1)

    monkeypatch.setattr("app.handlers.run_turn", fake_turn)
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    reply = await handle_message(_msg("¿cómo va mi parcela?"), trace_id="t-2")
    assert reply.text == "respuesta del agente"


async def test_every_turn_is_audited(db_pool, monkeypatch):
    async def fake_turn(text, budget):
        return TurnResult("ok", "ok", "m", (), 1, 1)

    monkeypatch.setattr("app.handlers.run_turn", fake_turn)
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")
    await handle_message(_msg("hola"), trace_id="t-audit")

    async with db_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT tenant_id, outcome FROM agent_turn_audit WHERE trace_id='t-audit'")
    assert row["tenant_id"] == "tenant_a"


async def test_quota_block_short_circuits_before_the_model(db_pool, monkeypatch):
    """A blocked turn must not reach the model.

    The cap exists to stop spend; calling the model and then refusing would spend
    the money it exists to save.
    """
    called = {"n": 0}

    async def fake_turn(text, budget):
        called["n"] += 1
        return TurnResult("no debería", "ok", "m", (), 1, 1)

    async def always_blocked(*a, **k):
        return "tenant_daily"

    monkeypatch.setattr("app.handlers.run_turn", fake_turn)
    monkeypatch.setattr("app.handlers.check_and_count", always_blocked)
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    reply = await handle_message(_msg("hola"), trace_id="t-q")
    assert called["n"] == 0
    assert reply.text


async def test_unlinked_user_never_reaches_the_model(db_pool, monkeypatch):
    """The oldest cost gate in this module, now with money behind it."""
    called = {"n": 0}

    async def fake_turn(text, budget):
        called["n"] += 1
        return TurnResult("no", "ok", "m", (), 1, 1)

    monkeypatch.setattr("app.handlers.run_turn", fake_turn)
    reply = await handle_message(_msg("hola", user="999"), trace_id="t-x")
    assert called["n"] == 0
    assert reply.text == handlers.NOT_LINKED


# --- Reinforcements (reviews 7 & 8) ------------------------------------------


async def test_settings_map_to_the_turn_budget(db_pool, monkeypatch, clear_settings_cache):
    """Wiring is the point of this task: get_settings() must translate into the
    four TurnBudget fields exactly. A silent permutation here would survive
    every behavioural test, so the budget object itself is inspected."""
    seen = {}

    async def capture_budget(text, budget):
        seen["budget"] = budget
        return TurnResult("ok", "ok", "m", (), 1, 1)

    monkeypatch.setenv("MAX_ITERATIONS", "3")
    monkeypatch.setenv("MAX_TOOL_CALLS", "5")
    monkeypatch.setenv("MAX_TOKENS_PER_TURN", "7777")
    monkeypatch.setenv("TURN_TIMEOUT_SECONDS", "11")
    get_settings.cache_clear()

    monkeypatch.setattr("app.handlers.run_turn", capture_budget)
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")
    await handle_message(_msg("hola"), trace_id="t-a")

    budget = seen["budget"]
    assert budget._max_iterations == 3
    assert budget._max_tool_calls == 5
    assert budget._max_tokens == 7777
    assert budget._timeout_s == 11


async def test_blocked_turn_leaves_no_audit_row(db_pool, monkeypatch):
    """Decision 3b: a blocked turn is refused before the model, so it never
    reaches record_turn and must leave no audit row for that trace."""
    async def always_blocked(*a, **k):
        return "tenant_daily"

    async def fake_turn(text, budget):
        return TurnResult("no", "ok", "m", (), 1, 1)

    monkeypatch.setattr("app.handlers.check_and_count", always_blocked)
    monkeypatch.setattr("app.handlers.run_turn", fake_turn)
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    reply = await handle_message(_msg("hola"), trace_id="t-blocked")
    assert reply.text == handlers.QUOTA_TEXT

    async with db_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM agent_turn_audit WHERE trace_id='t-blocked'")
    assert n == 0


async def test_handled_turn_records_a_full_audit_row(db_pool, monkeypatch):
    """Review 7 minor 1: every column of the audit row is pinned.

    The fake turn sleeps so the measured latency is non-zero; a hardcoded
    latency_ms=0 in the wiring would otherwise satisfy a `>= 0` assertion
    and the mutation would survive.
    """
    async def fake_turn(text, budget):
        await asyncio.sleep(0.02)
        return TurnResult("respuesta", "ok", "model-1", (), 10, 5)

    monkeypatch.setattr("app.handlers.run_turn", fake_turn)
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")
    await handle_message(_msg("hola"), trace_id="t-audit-full")

    async with db_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT tenant_id, user_id, channel, channel_user_id, inbound_text, "
            "outcome, latency_ms, model, tokens_prompt, tokens_completion, "
            "reply_text FROM agent_turn_audit WHERE trace_id='t-audit-full'")
    assert row["tenant_id"] == "tenant_a"
    assert row["user_id"] == "user_a"
    assert row["channel"] == "telegram"
    assert row["channel_user_id"] == "42"
    assert row["inbound_text"] == "hola"
    assert row["outcome"] == "ok"
    assert row["latency_ms"] > 0
    assert row["model"] == "model-1"
    assert row["tokens_prompt"] == 10
    assert row["tokens_completion"] == 5
    assert row["reply_text"] == "respuesta"


async def test_latency_clock_is_monotonic_by_provenance(db_pool, monkeypatch):
    """Latency must be measured with time.monotonic, not time.time.

    A wall-clock reading is wrong twice over for a spend/audit metric: an
    NTP step or DST change can make it negative, and it is not the clock the
    budget's own deadline uses. Provenance is pinned by injecting a clock
    whose `monotonic()` and `time()` return different values and asserting
    the recorded latency comes from `monotonic()`. Rewriting the call site
    in handlers.py to `time.time()` would make this go red (latency would
    read 0 instead of 250).
    """
    monotonic_ticks = [100.0, 100.25]
    time_ticks = [50_000.0, 50_000.0]

    class FakeClock:
        def monotonic(self) -> float:
            return monotonic_ticks.pop(0)

        def time(self) -> float:
            return time_ticks.pop(0)

    monkeypatch.setattr(handlers, "time", FakeClock())

    async def fake_turn(text, budget):
        return TurnResult("respuesta", "ok", "model-1", (), 10, 5)

    monkeypatch.setattr("app.handlers.run_turn", fake_turn)
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")
    await handle_message(_msg("hola"), trace_id="t-clock")

    async with db_pool.acquire() as c:
        latency = await c.fetchval(
            "SELECT latency_ms FROM agent_turn_audit WHERE trace_id='t-clock'")
    assert latency == 250


async def test_audit_failure_keeps_reply_and_hides_message_text(
        db_pool, monkeypatch, caplog):
    """Review 7 minor 2: a failing audit write must neither lose the reply nor
    leak the farmer's message text into the logs."""
    canary = "CANARY-9f3a-do-not-log-this-text"

    async def fake_turn(text, budget):
        return TurnResult("ok", "ok", "m", (), 1, 1)

    async def broken_pool():
        raise RuntimeError("db gone")

    monkeypatch.setattr("app.handlers.run_turn", fake_turn)
    monkeypatch.setattr("app.audit.repository.get_pool", broken_pool)

    caplog.set_level(logging.INFO)
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")
    reply = await handle_message(_msg(canary), trace_id="t-canary")

    assert reply.text == "ok"
    assert canary not in caplog.text
