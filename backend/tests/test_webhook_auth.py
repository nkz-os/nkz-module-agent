import ast
import asyncio
import hmac as stdlib_hmac
import inspect
import threading
import time
import uuid

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


def test_parse_failure_is_logged_and_does_not_propagate(client, monkeypatch, caplog):
    """A raising adapter.parse() must not vanish silently.

    This is exactly the case the FIX C except block exists for: a malformed
    update can make the adapter raise (e.g. a non-numeric `date` field
    reaching datetime.fromtimestamp), not just return None for a kind it
    doesn't handle. By the time it does, the platform has already received
    its 200 — nothing retries — so losing this exception would lose the
    turn with zero record it ever existed. `msg` is never assigned on this
    path, so this also proves the failure log's placeholder fallback
    doesn't itself blow up trying to read attributes off a None msg.
    """
    def raising_parse(raw):
        raise TypeError("boom: non-numeric date")

    fixed_trace_id = uuid.UUID("11111111-1111-1111-1111-111111111111")

    monkeypatch.setattr("app.api.webhook.adapter.parse", raising_parse)
    monkeypatch.setattr("app.api.webhook.uuid.uuid4", lambda: fixed_trace_id)

    with caplog.at_level("ERROR"):
        r = client.post(
            "/api/agent/webhook/telegram",
            json=_update(), headers={HEADER: "s3cret-for-tests"},
        )

    assert r.status_code == 200
    assert "turn_failed" in caplog.text
    assert str(fixed_trace_id) in caplog.text


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


def _is_provenance_clean_return(value: ast.expr | None) -> bool:
    """True if a `return` value is a bare literal or the compare_digest call.

    Used by test_secret_comparison_uses_hmac_compare_digest to check RETURN
    PROVENANCE, not just presence: AST presence of a call somewhere in the
    function body is not the same as that call's result being what the
    function actually returns. A decoy call
    (`hmac.compare_digest(b"decoy", b"decoy")`, result discarded) followed by
    `return provided == expected` still contains a node that unparses to
    "hmac.compare_digest" — a presence-only check stays green while the real
    decision is an insecure `==`. This walks every `return` in the function
    and requires each one to be either a literal constant (the fail-closed
    branches legitimately `return False`) or the compare_digest call itself,
    optionally wrapped in `bool(...)`.
    """
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, ast.Call):
        func_text = ast.unparse(value.func)
        if func_text == "hmac.compare_digest":
            return True
        if func_text == "bool" and len(value.args) == 1:
            inner = value.args[0]
            if isinstance(inner, ast.Call) and ast.unparse(inner.func) == "hmac.compare_digest":
                return True
    return False


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

    Also checked: the name `hmac` bound inside the webhook module IS the
    real standard-library module object. The AST walk alone inspects the
    call site's *syntax* only — the text `hmac.compare_digest(...)` — never
    what `hmac` is actually bound to at runtime. A shim placed right after
    the genuine `import hmac` (e.g. `hmac = _FakeHmacShim()`, whose own
    `compare_digest` uses `==`) still produces a call that unparses to
    "hmac.compare_digest", so the AST check alone passes while the
    comparison is genuinely insecure. The identity check closes that.

    Also checked: RETURN PROVENANCE, not just call presence. "A call to
    hmac.compare_digest appears somewhere in this function" is not the same
    claim as "the function's answer IS that call's result, unconditionally".
    Two shapes defeat presence-only checking:
      - a debug/decoy bypass added before the real comparison
        (`if provided.startswith("dbg-"): return provided[4:] == expected`) —
        adds a `return` whose value is a `Compare` node, not touching
        compare_digest at all;
      - a decoy call with its result discarded
        (`hmac.compare_digest(b"decoy", b"decoy")` as a bare expression
        statement, followed by `return (provided or "") == expected`) — the
        function body still contains a node that unparses to
        "hmac.compare_digest", but no `return` actually uses it.
    `_is_provenance_clean_return` walks every `ast.Return` in the function
    and requires each one to be a literal constant (the fail-closed branches
    legitimately `return False`) or the compare_digest call itself (bare, or
    wrapped in `bool(...)`) — nothing else.

    KNOWN, ACCEPTED LIMITATION, encoded deliberately narrow: this test
    inspects `_authorised`'s OWN call site and its own `return` statements'
    shapes via the AST — it does not run the code, and it does not follow
    the comparison into a helper. If the comparison is ever moved into a
    small helper function (e.g. `return _secure_equal(provided, expected)`),
    this test will fail even though that refactor is perfectly safe, because
    the call site would then read `_secure_equal(...)`, not
    `hmac.compare_digest(...)`, and no `return` in `_authorised` would match
    the allowed shapes any more. That failure means THIS TEST needs updating
    to inspect the new call site, not that the code regressed. The reasoning
    for this whole test's shape lives in `_authorised`'s own docstring too,
    so the boundary survives independently of this file.

    Reassigning `hmac.compare_digest` in place on the real standard-library
    module object (`hmac.compare_digest = insecure_fn`, no rebinding of the
    `hmac` name) is NOT covered by any check here — see the
    DOCUMENTED-NOT-ENFORCED paragraph in `_authorised`'s own docstring for
    why: it is runtime monkeypatching indistinguishable from the real thing
    from this test's vantage point, ruled out of scope rather than chased.
    """
    assert webhook_module.hmac is stdlib_hmac

    tree = ast.parse(inspect.getsource(webhook_module._authorised))

    calls = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "hmac.compare_digest" in calls

    returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]
    assert returns, "_authorised has no return statements to check"
    for node in returns:
        assert _is_provenance_clean_return(node.value), (
            "a return statement in _authorised does not provably come from "
            "hmac.compare_digest or a literal constant: "
            f"`return {ast.unparse(node.value) if node.value is not None else None}`"
        )


def test_reply_delivery_does_not_block_concurrent_acknowledgements(monkeypatch):
    """FIX H: prove the real async reply path does not block the event loop.

    Every other test that reaches send_reply monkeypatches send_reply itself,
    so the real `async with httpx.AsyncClient(...)` body never runs anywhere
    else in this suite — it proves send_reply is CALLED with the right
    payload, not that the call doesn't block. The defect FIX B closed was a
    synchronous httpx.post() eating up to 10s of the event loop's time,
    stalling every OTHER delivery's acknowledgement in flight — exactly the
    condition that invites the retries this whole design exists to avoid.

    This exercises the real send_reply against a controlled transport and
    proves concurrency at the event-loop level: two deliveries fire together
    on a SHARED event loop — TestClient is used as a context manager so both
    `client.post()` calls run their ASGI cycle on the SAME anyio portal
    (Starlette hands out a fresh portal, and thus a fresh event loop, per
    request UNLESS the client is entered as `with TestClient(app) as c:`,
    which pins one portal for every request made inside the block) — each
    triggering a reply-send that sleeps DELAY seconds. If that sleep is
    genuinely async (`await asyncio.sleep`), the loop interleaves both tasks
    and total wall time for both deliveries together stays close to ONE
    DELAY. If it were a blocking call instead (`time.sleep`, standing in for
    the old sync httpx.post — a real blocking socket call ties up the
    thread identically), the second delivery's coroutine cannot even start
    running until the first one's blocking call releases the single shared
    event-loop thread, so total wall time climbs to close to TWO DELAYs.

    Both httpx.post (sync) and httpx.AsyncClient (async) are patched so this
    same test body proves RED against the pre-fix synchronous form and GREEN
    against the current async form without editing the test in between.
    """
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret-for-tests")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-for-tests")
    get_settings.cache_clear()

    DELAY = 0.3

    async def fake_claim(key):
        return True

    async def fake_handle(msg, trace_id):
        from app.domain.messages import OutboundMessage
        return OutboundMessage(text="ok")

    def fake_sync_post(*args, **kwargs):
        # Stand-in for the pre-fix synchronous httpx.post(): real blocking
        # network I/O ties up the running thread for its duration exactly
        # like time.sleep does, from the event loop's point of view.
        time.sleep(DELAY)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, *args, **kwargs):
            await asyncio.sleep(DELAY)

    monkeypatch.setattr("app.api.webhook.claim_update", fake_claim)
    monkeypatch.setattr("app.api.webhook.handle_message", fake_handle)
    monkeypatch.setattr("app.api.webhook.httpx.post", fake_sync_post)
    monkeypatch.setattr("app.api.webhook.httpx.AsyncClient", FakeAsyncClient)

    app = create_app()
    results = {}

    with TestClient(app) as shared_client:

        def fire(key, update_id):
            start = time.monotonic()
            r = shared_client.post(
                "/api/agent/webhook/telegram",
                json=_update(update_id), headers={HEADER: "s3cret-for-tests"},
            )
            results[key] = r.status_code

        overall_start = time.monotonic()
        t1 = threading.Thread(target=fire, args=("a", 101))
        t2 = threading.Thread(target=fire, args=("b", 102))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        overall_elapsed = time.monotonic() - overall_start

    assert results["a"] == 200
    assert results["b"] == 200
    # Concurrent: ~1 DELAY total. Serialised: ~2 DELAYs total. The threshold
    # sits clearly between the two, with margin for scheduling overhead.
    assert overall_elapsed < DELAY * 1.6, (
        f"acknowledgements serialised: {overall_elapsed:.2f}s wall time for "
        f"two concurrent deliveries each carrying a {DELAY}s reply-send"
    )

    get_settings.cache_clear()
