from app.channels.telegram import TelegramAdapter, start_payload
from app.domain.messages import Citation, OutboundMessage
from datetime import datetime, timezone

adapter = TelegramAdapter()


def _text_update(
    text: str, update_id: int = 100, user_id: int = 42, chat_id: int = -100999
) -> dict:
    # chat_id defaults to a group id DIFFERENT from user_id on purpose: a
    # fixture where sender and conversation coincide cannot catch a
    # regression back to replying at the sender instead of the conversation.
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1758499200,
            "chat": {"id": chat_id, "type": "group"},
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


def test_parse_carries_the_conversation_id_separately_from_the_sender():
    """The conversation (delivery target) and the sender (identity) are
    different fields, and — per the fixture default — different values.
    """
    msg = adapter.parse(_text_update("hola"))
    assert msg is not None
    assert msg.channel_user_id == "42"
    assert msg.conversation_id == "-100999"
    assert msg.conversation_id != msg.channel_user_id


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
    assert msg.conversation_id == "-999"


def test_parse_ignores_updates_missing_chat():
    raw = _text_update("hola")
    del raw["message"]["chat"]
    assert adapter.parse(raw) is None


def test_parse_ignores_updates_with_non_dict_chat():
    raw = _text_update("hola")
    raw["message"]["chat"] = "not-a-dict"
    assert adapter.parse(raw) is None


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
    """The platform sends many update kinds; this adapter handles only `message`."""
    assert adapter.parse({"update_id": 1, "edited_channel_post": {}}) is None
    assert adapter.parse(
        {"update_id": 2, "edited_message": {"message_id": 3, "text": "edit"}}
    ) is None
    assert adapter.parse(
        {"update_id": 3, "callback_query": {"id": "cb1", "from": {"id": 42}}}
    ) is None
    assert adapter.parse({"update_id": 4}) is None


def test_parse_ignores_update_missing_update_id():
    raw = _text_update("hola")
    del raw["update_id"]
    assert adapter.parse(raw) is None


def test_parse_ignores_voice_missing_file_id():
    raw = _text_update("")
    del raw["message"]["text"]
    raw["message"]["voice"] = {"duration": 5, "mime_type": "audio/ogg"}
    assert adapter.parse(raw) is None


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
