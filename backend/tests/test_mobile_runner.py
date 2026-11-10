from datetime import datetime, timezone

import pytest

from app.agent.loop import TurnResult
from app.domain.session import SessionContext
from app.mobile import runner

pytestmark = pytest.mark.asyncio

CTX = SessionContext("tenant_a", "u1", ("Farmer",), "mobile", "u1", "trace-1")


def _result(text="hi", outcome="ok", **kw):
    return TurnResult(
        text=text, outcome=outcome, model="m", tool_calls=(),
        tokens_prompt=1, tokens_completion=1, **kw,
    )


@pytest.fixture
def calls(monkeypatch):
    log = []

    async def running(turn_id):
        log.append(("running", turn_id))
        return True

    async def done(turn_id, reply_text, citations, outcome):
        log.append(("done", turn_id, reply_text, citations, outcome))

    async def failed(turn_id, error_code, outcome):
        log.append(("failed", turn_id, error_code, outcome))

    monkeypatch.setattr(runner.turns, "mark_running", running)
    monkeypatch.setattr(runner.turns, "mark_done", done)
    monkeypatch.setattr(runner.turns, "mark_failed", failed)
    return log


async def test_ok_marks_done_with_citation_dicts(monkeypatch, calls):
    seen = {}
    when = datetime(2026, 1, 2, tzinfo=timezone.utc)

    async def fake(session, text, trace_id, context_note=None):
        seen["text"] = text
        seen["note"] = context_note
        return TurnResult(
            text="answer", outcome="ok", model="m", tool_calls=(),
            tokens_prompt=1, tokens_completion=1,
            citations=({"source": "orion", "retrieved_at": when.isoformat(), "detail": "d"}, {"source": "x"}),
        )

    monkeypatch.setattr(runner, "execute_turn", fake)
    await runner.run_mobile_turn("t1", CTX, "question", "Parcel: P1")
    assert seen["text"] == "question"
    assert seen["note"] == "Parcel: P1"
    assert calls[0] == ("running", "t1")
    assert calls[1] == (
        "done", "t1", "answer",
        [{"source": "orion", "retrieved_at": when.isoformat(), "detail": "d"},
         {"source": "x"}],
        "ok",
    )


async def test_no_parcel_context_passes_text_unchanged(monkeypatch, calls):
    seen = {}

    async def fake(session, text, trace_id, context_note=None):
        seen["text"] = text
        seen["note"] = context_note
        return _result()

    monkeypatch.setattr(runner, "execute_turn", fake)
    await runner.run_mobile_turn("t1", CTX, "question", None)
    assert seen["text"] == "question"
    assert seen["note"] is None
    assert calls[1] == ("done", "t1", "hi", [], "ok")


async def test_budget_exhausted_is_done(monkeypatch, calls):
    async def fake(session, text, trace_id, context_note=None):
        return _result(outcome="budget_exhausted")

    monkeypatch.setattr(runner, "execute_turn", fake)
    await runner.run_mobile_turn("t1", CTX, "q", None)
    assert calls[1][0] == "done" and calls[1][4] == "budget_exhausted"


async def test_unconfigured_marks_failed(monkeypatch, calls):
    async def fake(session, text, trace_id, context_note=None):
        return _result(outcome="unconfigured")

    monkeypatch.setattr(runner, "execute_turn", fake)
    await runner.run_mobile_turn("t1", CTX, "q", None)
    assert calls[1] == ("failed", "t1", "llm_unconfigured", "unconfigured")


async def test_error_outcome_marks_failed(monkeypatch, calls):
    async def fake(session, text, trace_id, context_note=None):
        return _result(outcome="error")

    monkeypatch.setattr(runner, "execute_turn", fake)
    await runner.run_mobile_turn("t1", CTX, "q", None)
    assert calls[1] == ("failed", "t1", "error", "error")


async def test_exception_marks_failed_and_never_raises(monkeypatch, calls):
    async def boom(session, text, trace_id, context_note=None):
        raise RuntimeError("x")

    monkeypatch.setattr(runner, "execute_turn", boom)
    await runner.run_mobile_turn("t1", CTX, "q", None)
    assert calls[-1] == ("failed", "t1", "error", None)


async def test_not_queued_skips_the_model_call(monkeypatch, calls):
    async def running(turn_id):
        calls.append(("running", turn_id))
        return False

    async def fake(session, text, trace_id, context_note=None):
        raise AssertionError("model must not be called")

    monkeypatch.setattr(runner.turns, "mark_running", running)
    monkeypatch.setattr(runner, "execute_turn", fake)
    await runner.run_mobile_turn("t1", CTX, "q", None)
    assert calls == [("running", "t1")]
