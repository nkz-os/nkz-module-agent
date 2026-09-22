from app.channels.telegram import TelegramAdapter, start_payload
from app.domain.messages import Citation, OutboundMessage
from datetime import datetime, timezone

adapter = TelegramAdapter()


def _text_update(text: str, update_id: int = 100, user_id: int = 42) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1758499200,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False},
            "text": text,
        },
    }


def test_parse_extracts_identity_and_text():
    msg = adapter.parse(_text_update("hola"))
    assert msg is not None
    assert msg.channel == "telegram"
    assert msg.channel_user_id == "42"
    assert msg.text == "hola"
    assert msg.voice is None


def test_idempotency_key_is_channel_scoped():
    msg = adapter.parse(_text_update("hola", update_id=7))
    assert msg is not None
    assert msg.idempotency_key == "telegram:7"


def test_parse_reads_sender_not_chat():
    """In a group the chat id is not the sender; identity must follow the sender."""
    raw = _text_update("hola")
    raw["message"]["chat"]["id"] = -999
    msg = adapter.parse(raw)
    assert msg is not None
    assert msg.channel_user_id == "42"


def test_parse_voice_note_yields_a_reference():
    raw = _text_update("")
    del raw["message"]["text"]
    raw["message"]["voice"] = {
        "file_id": "AwACAgQ", "duration": 5, "mime_type": "audio/ogg",
    }
    msg = adapter.parse(raw)
    assert msg is not None
    assert msg.text is None
    assert msg.voice is not None
    assert msg.voice.file_id == "AwACAgQ"
    assert msg.voice.duration_s == 5


def test_parse_ignores_updates_without_a_message():
    assert adapter.parse({"update_id": 1, "edited_channel_post": {}}) is None


def test_render_targets_the_chat_and_carries_text():
    payload = adapter.render(OutboundMessage(text="hola"), chat_id="42")
    assert payload["chat_id"] == "42"
    assert payload["text"] == "hola"


def test_render_appends_citations_as_readable_text():
    when = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    msg = OutboundMessage(
        text="NDVI 0.72",
        citations=(Citation(source="vegetation-health", retrieved_at=when),),
    )
    payload = adapter.render(msg, chat_id="42")
    assert "vegetation-health" in payload["text"]
    assert "2026-09-22" in payload["text"]


def test_start_payload_extracts_the_token():
    msg = adapter.parse(_text_update("/start abc-123"))
    assert msg is not None
    assert start_payload(msg) == "abc-123"


def test_start_without_payload_returns_none():
    msg = adapter.parse(_text_update("/start"))
    assert msg is not None
    assert start_payload(msg) is None


def test_start_payload_ignores_other_commands():
    msg = adapter.parse(_text_update("/help abc"))
    assert msg is not None
    assert start_payload(msg) is None
