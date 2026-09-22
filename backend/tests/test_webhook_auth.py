import ast
import inspect

import pytest
from fastapi.testclient import TestClient

from app.api import webhook as webhook_module
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


def test_prefix_of_the_secret_is_rejected(client):
    """A truncated comparison (e.g. only the first N bytes) must not pass."""
    r = client.post(
        "/api/agent/webhook/telegram", json=_update(), headers={HEADER: "s3c"}
    )
    assert r.status_code == 401


def test_differently_cased_secret_is_rejected(client):
    """A case-insensitive comparison must not pass."""
    r = client.post(
        "/api/agent/webhook/telegram",
        json=_update(),
        headers={HEADER: "S3CRET-FOR-TESTS"},
    )
    assert r.status_code == 401


def test_non_ascii_header_is_rejected_not_500(client):
    """A non-ASCII header byte must fail closed (401), not crash (500).

    hmac.compare_digest raises TypeError on a non-ASCII str operand. The
    attacker side of this is already safe either way (an unhandled 500 still
    rejects the request) — what matters is the mirror case: a misconfigured
    non-ASCII TELEGRAM_WEBHOOK_SECRET must not turn every legitimate
    delivery into a permanent retry storm against a 500.
    """
    r = client.post(
        "/api/agent/webhook/telegram", json=_update(), headers={HEADER: b"\xff"}
    )
    assert r.status_code == 401


def test_auth_happens_before_the_body_is_parsed(client):
    """A security property: an unauthenticated request must be rejected
    before its body is even looked at. If body-parsing ever moved ahead of
    the secret check, an unauthenticated caller with an unparseable body
    would hit the malformed-body path and get an undeserved 200.
    """
    r = client.post(
        "/api/agent/webhook/telegram",
        content=b"not json at all {{{",
        headers={"Content-Type": "application/json"},  # no secret header at all
    )
    assert r.status_code == 401


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

    async def fake_send_reply(payload):
        return None

    monkeypatch.setattr("app.api.webhook.claim_update", fake_claim)
    monkeypatch.setattr("app.api.webhook.handle_message", fake_handle)
    monkeypatch.setattr("app.api.webhook.send_reply", fake_send_reply)

    h = {HEADER: "s3cret-for-tests"}
    r1 = client.post("/api/agent/webhook/telegram", json=_update(9), headers=h)
    r2 = client.post("/api/agent/webhook/telegram", json=_update(9), headers=h)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert handle_calls == ["telegram:9"]


def test_failed_turn_is_logged_and_does_not_propagate(client, monkeypatch, caplog):
    """A raising handle_message must not vanish silently.

    claim_update has already committed by the time handle_message can raise,
    so the update is marked processed and the platform will never retry it —
    on a /start turn the link token may already be burned too. Losing the
    exception here would lose the turn with zero record it ever happened.
    """
    async def fake_claim(key):
        return True

    async def raising_handle(msg, trace_id):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.api.webhook.claim_update", fake_claim)
    monkeypatch.setattr("app.api.webhook.handle_message", raising_handle)

    with caplog.at_level("ERROR"):
        r = client.post(
            "/api/agent/webhook/telegram",
            json=_update(), headers={HEADER: "s3cret-for-tests"},
        )

    assert r.status_code == 200
    assert "turn_failed" in caplog.text
    assert "telegram:1" in caplog.text


def test_reply_is_delivered(client, monkeypatch):
    """A successful turn must actually send a reply back to the channel."""
    sent = []

    async def fake_claim(key):
        return True

    async def fake_handle(msg, trace_id):
        from app.domain.messages import OutboundMessage
        return OutboundMessage(text="ok")

    async def fake_send_reply(payload):
        sent.append(payload)

    monkeypatch.setattr("app.api.webhook.claim_update", fake_claim)
    monkeypatch.setattr("app.api.webhook.handle_message", fake_handle)
    monkeypatch.setattr("app.api.webhook.send_reply", fake_send_reply)

    r = client.post(
        "/api/agent/webhook/telegram",
        json=_update(), headers={HEADER: "s3cret-for-tests"},
    )

    assert r.status_code == 200
    assert sent == [{"chat_id": "42", "text": "ok"}]


def test_response_returns_before_the_work_runs(monkeypatch):
    """The whole design (fast ack, work out of band) rests on this ordering.

    TestClient runs the ASGI app in-process, so wall-clock timing can't show
    a real race; what differs between `background.add_task(...)` and
    `await process_update(...)` inline is the ORDER of two events within the
    same call. Starlette's Response.__call__ sends the body, then runs
    background tasks — a background task always executes strictly after the
    response is sent. This hooks the raw ASGI `send` to record "response
    sent" and monkeypatches process_update to record "work ran", then checks
    the order. Awaiting the work inline would flip it.
    """
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret-for-tests")
    get_settings.cache_clear()

    events = []

    async def fake_process(raw):
        events.append("work_ran")

    monkeypatch.setattr("app.api.webhook.process_update", fake_process)

    fastapi_app = create_app()

    async def probe(scope, receive, send):
        async def send_wrapper(message):
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                events.append("response_sent")
            await send(message)

        await fastapi_app(scope, receive, send_wrapper)

    probe_client = TestClient(probe)
    r = probe_client.post(
        "/api/agent/webhook/telegram",
        json=_update(), headers={HEADER: "s3cret-for-tests"},
    )

    assert r.status_code == 200
    assert events == ["response_sent", "work_ran"]

    get_settings.cache_clear()


def test_secret_comparison_uses_hmac_compare_digest():
    """Mechanism check, not behaviour.

    Swapping hmac.compare_digest for `==` is functionally invisible to any
    test that only inspects responses — the difference is comparison timing,
    a side channel no output-based assertion can see (a `==` scan can return
    fractionally faster on an early mismatched byte, letting a patient
    attacker recover the secret one byte at a time). The property has
    nowhere else to live, so it is asserted directly against the source.

    Checked via the AST's actual Call nodes, not a raw substring search: the
    function's own docstring names "hmac.compare_digest" in prose, so a
    naive `"hmac.compare_digest" in source` string check would stay green
    even after the real comparison was swapped to `==`.
    """
    tree = ast.parse(inspect.getsource(webhook_module._authorised))
    calls = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "hmac.compare_digest" in calls
