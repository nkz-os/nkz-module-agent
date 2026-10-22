"""Hard gate: voice notes and contentless messages never reach quota or model."""

import logging
from datetime import datetime, timezone

import pytest

from app import handlers
from app.agent.loop import TurnResult
from app.domain.messages import InboundMessage, VoiceRef
from app.handlers import handle_message
from app.identity import service
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

FILE_ID = "voice-file-id-SECRET-123"


def _msg(text, voice=None, user="42") -> InboundMessage:
    return InboundMessage(
        channel="telegram", channel_user_id=user, conversation_id=user, text=text,
        voice=voice, idempotency_key=f"telegram:{user}",
        received_at=datetime.now(timezone.utc),
    )


def _voice() -> VoiceRef:
    return VoiceRef(file_id=FILE_ID, duration_s=7, mime_type="audio/ogg")


@pytest.fixture
def counters(monkeypatch):
    c = {"turn": 0, "quota": 0, "texts": []}

    async def fake_turn(text, budget, ctx):
        c["turn"] += 1
        c["texts"].append(text)
        return TurnResult(text="respuesta", outcome="ok", model="m", tool_calls=(),
                          tokens_prompt=1, tokens_completion=1)

    async def fake_quota(tenant_id, channel, channel_user_id):
        c["quota"] += 1

    monkeypatch.setattr("app.handlers.run_turn", fake_turn)
    monkeypatch.setattr("app.handlers.check_and_count", fake_quota)
    return c


async def _link(user="42"):
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}", user=user), trace_id="t-link")


async def _audit(db_pool, trace_id):
    async with db_pool.acquire() as c:
        return await c.fetch(
            "SELECT outcome, reply_text, inbound_text, model, tokens_prompt, "
            "tokens_completion FROM agent_turn_audit WHERE trace_id=$1", trace_id)


async def test_voice_note_is_refused_before_quota_and_model(
        db_pool, counters, caplog):
    await _link()
    with caplog.at_level(logging.DEBUG):
        reply = await handle_message(_msg(None, voice=_voice()), trace_id="t-voice")

    assert reply.text == handlers.VOICE_NOT_READY_TEXT
    assert counters["turn"] == 0, "the provider was invoked with a voice message"
    assert counters["quota"] == 0, "a voice note consumed quota"
    rows = await _audit(db_pool, "t-voice")
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "refused"
    assert row["reply_text"] == "voice_unsupported"
    assert row["inbound_text"] is None
    assert row["model"] is None
    assert row["tokens_prompt"] == 0
    assert row["tokens_completion"] == 0
    assert FILE_ID not in caplog.text


@pytest.mark.parametrize("text", [None, "   "])
async def test_contentless_message_is_refused_before_quota_and_model(
        db_pool, counters, text):
    await _link()

    reply = await handle_message(_msg(text), trace_id="t-empty")

    assert reply.text == handlers.EMPTY_MESSAGE_TEXT
    assert counters["turn"] == 0
    assert counters["quota"] == 0
    rows = await _audit(db_pool, "t-empty")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["reply_text"] == "empty_message"
    assert rows[0]["inbound_text"] is None


async def test_voice_with_text_takes_the_normal_text_path(db_pool, counters):
    await _link()

    reply = await handle_message(_msg("hola", voice=_voice()), trace_id="t-both")

    assert counters["turn"] == 1
    assert counters["texts"] == ["hola"]
    assert reply.text == "respuesta"


async def test_unlinked_voice_gets_not_linked_and_no_audit(db_pool, counters):
    reply = await handle_message(_msg(None, voice=_voice(), user="999"),
                                 trace_id="t-unlinked")

    assert reply.text == handlers.NOT_LINKED
    assert counters["turn"] == 0
    assert counters["quota"] == 0
    assert await _audit(db_pool, "t-unlinked") == []
