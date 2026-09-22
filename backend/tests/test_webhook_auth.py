import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app

HEADER = "X-Telegram-Bot-Api-Secret-Token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret-for-tests")
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


def _update(update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1, "date": 1758499200,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "hola",
        },
    }


def test_missing_secret_is_rejected(client):
    r = client.post("/api/agent/webhook/telegram", json=_update())
    assert r.status_code == 401


def test_wrong_secret_is_rejected(client):
    r = client.post(
        "/api/agent/webhook/telegram", json=_update(), headers={HEADER: "wrong"}
    )
    assert r.status_code == 401


def test_rejection_does_not_log_the_body(client, caplog):
    """An unauthenticated caller must not be able to write chosen text to logs."""
    with caplog.at_level("DEBUG"):
        client.post(
            "/api/agent/webhook/telegram",
            json={"update_id": 1, "message": {"text": "CANARY-STRING"}},
            headers={HEADER: "wrong"},
        )
    assert "CANARY-STRING" not in caplog.text


def test_correct_secret_is_accepted(client, monkeypatch):
    seen = []

    async def fake_process(raw):
        seen.append(raw)

    monkeypatch.setattr("app.api.webhook.process_update", fake_process)

    r = client.post(
        "/api/agent/webhook/telegram",
        json=_update(), headers={HEADER: "s3cret-for-tests"},
    )

    assert r.status_code == 200
    assert seen and seen[0]["update_id"] == 1


def test_unset_secret_rejects_everything(monkeypatch):
    """Fail closed: an unconfigured webhook must not accept traffic."""
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    c = TestClient(create_app())

    assert c.post("/api/agent/webhook/telegram", json=_update()).status_code == 401
    assert c.post(
        "/api/agent/webhook/telegram", json=_update(), headers={HEADER: ""}
    ).status_code == 401

    get_settings.cache_clear()


def test_non_object_json_body_is_acknowledged_and_ignored(client, monkeypatch):
    """A body that is valid JSON but not an update object must not crash or retry-loop."""
    handle_calls = []

    async def fake_handle(msg, trace_id):
        handle_calls.append(msg)
        from app.domain.messages import OutboundMessage
        return OutboundMessage(text="ok")

    monkeypatch.setattr("app.api.webhook.handle_message", fake_handle)

    r = client.post(
        "/api/agent/webhook/telegram",
        json=["not", "an", "object"],
        headers={HEADER: "s3cret-for-tests"},
    )

    assert r.status_code == 200
    assert handle_calls == []


def test_non_json_body_is_acknowledged_and_ignored(client, monkeypatch):
    """A body that is not JSON at all must not crash or retry-loop."""
    handle_calls = []

    async def fake_handle(msg, trace_id):
        handle_calls.append(msg)
        from app.domain.messages import OutboundMessage
        return OutboundMessage(text="ok")

    monkeypatch.setattr("app.api.webhook.handle_message", fake_handle)

    r = client.post(
        "/api/agent/webhook/telegram",
        content=b"not json at all {{{",
        headers={HEADER: "s3cret-for-tests", "Content-Type": "application/json"},
    )

    assert r.status_code == 200
    assert handle_calls == []


def test_duplicate_update_is_processed_once(client, monkeypatch):
    """A retried delivery must not run the turn twice.

    claim_update is faked but kept faithful to the real contract (True for
    exactly the first caller per key); the assertion that matters is on
    handle_message call count — how many times the turn was handled, not
    how many times a claim was attempted.
    """
    seen_keys = []
    handle_calls = []

    async def fake_claim(key):
        first = key not in seen_keys
        seen_keys.append(key)
        return first

    async def fake_handle(msg, trace_id):
        from app.domain.messages import OutboundMessage
        handle_calls.append(msg.idempotency_key)
        return OutboundMessage(text="ok")

    monkeypatch.setattr("app.api.webhook.claim_update", fake_claim)
    monkeypatch.setattr("app.api.webhook.handle_message", fake_handle)
    monkeypatch.setattr("app.api.webhook.send_reply", lambda payload: None)

    h = {HEADER: "s3cret-for-tests"}
    r1 = client.post("/api/agent/webhook/telegram", json=_update(9), headers=h)
    r2 = client.post("/api/agent/webhook/telegram", json=_update(9), headers=h)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert handle_calls == ["telegram:9"]
