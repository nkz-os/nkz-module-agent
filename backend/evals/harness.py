"""Synthetic evaluation harness: load cases, run them, score them.

A case is one synthetic conversation turn plus assertions about what the agent
must (and must not) do. ``run_case`` drives the REAL ``run_turn`` /
``handle_message`` with only the edges replaced: the tool registry (synthetic
fixtures, real argument schemas), conversation memory (an in-memory store) and,
for dry runs, the model call (a stub). With a live provider the model call is
NOT patched: ``app.llm.provider.complete`` resolves the module's own
``LLM_MODEL`` / ``LLM_API_BASE`` / ``LLM_API_KEY`` environment.

Two assertions are heuristics, by design (no dependencies, no network):

* ``contains_citation``: the reply mentions one of the case fixtures' citation
  sources (case-insensitive) or contains an ISO date (YYYY-MM-DD).
* ``language``: stopword counting over es / en / eu word lists. The language
  with the most hits wins; a tie (including zero hits) is "undetermined" and
  fails. Approximate: short replies and proper nouns can mislead it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import handlers
from app.agent.budget import TurnBudget
from app.agent.loop import run_turn
from app.domain.messages import InboundMessage, VoiceRef
from app.domain.session import SessionContext
from app.llm.provider import LLMReply
from evals.fixtures import RETRIEVED_DATE, build_tools, citation_sources

Lang = Literal["es", "en", "eu"]
DEFAULT_BUDGET = {"max_iterations": 4, "max_tool_calls": 6, "max_tokens": 4000, "timeout_s": 30}
_GATE_CONSTANTS = {
    "VOICE_NOT_READY_TEXT": handlers.VOICE_NOT_READY_TEXT,
    "EMPTY_MESSAGE_TEXT": handlers.EMPTY_MESSAGE_TEXT,
}

# --------------------------------------------------------------------------
# Case schema
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedCall(_Strict):
    name: str
    args_contain: dict[str, Any] = Field(default_factory=dict)


class ReplyExpect(_Strict):
    contains_citation: bool | None = None
    language: Lang | None = None
    must_not_contain: list[str] = Field(default_factory=list)
    must_contain_any: list[str] = Field(default_factory=list)
    equals_constant: Literal["VOICE_NOT_READY_TEXT", "EMPTY_MESSAGE_TEXT"] | None = None


class Expect(_Strict):
    tool_calls: list[ExpectedCall] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    outcome: Literal["ok", "budget_exhausted", "unconfigured", "error", "refused"] = "ok"
    reply: ReplyExpect = Field(default_factory=ReplyExpect)


class MemoryMessage(_Strict):
    role: str
    content: str


class Case(_Strict):
    id: str
    input: str
    kind: Literal["text", "voice", "empty"] = "text"
    tools_available: list[str] = Field(default_factory=list)
    fixture_variant: Literal["plain", "injected"] = "plain"
    budget: dict[str, float] = Field(default_factory=dict)
    memory: list[MemoryMessage] = Field(default_factory=list)
    memory_owner: Literal["self", "other_tenant"] = "self"
    expect: Expect = Field(default_factory=Expect)
    weight: int = Field(default=1, ge=1)
    lang: Lang = "es"

    @model_validator(mode="after")
    def _check(self) -> Case:
        unknown = set(self.budget) - set(DEFAULT_BUDGET)
        if unknown:
            raise ValueError(f"unknown budget keys {sorted(unknown)}")
        if not self.has_assertion():
            raise ValueError(f"case {self.id!r} has no assertion")
        return self

    def has_assertion(self) -> bool:
        r = self.expect.reply
        return bool(
            self.expect.tool_calls
            or self.expect.forbidden_tools
            or self.expect.outcome != "ok"
            or r.contains_citation is not None
            or r.language
            or r.must_not_contain
            or r.must_contain_any
            or r.equals_constant
        )

    def turn_budget(self) -> TurnBudget:
        return TurnBudget(**{**DEFAULT_BUDGET, **self.budget})


def load_cases(directory: Path | str) -> list[Case]:
    """Parse every ``*.yaml`` in ``directory``; ids must be unique globally."""
    cases: list[Case] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for item in raw:
            cases.append(Case.model_validate(item))
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id!r}")
        seen.add(case.id)
    return cases


# --------------------------------------------------------------------------
# Heuristic assertions
# --------------------------------------------------------------------------

_STOPWORDS: dict[str, frozenset[str]] = {
    "es": frozenset(["el", "la", "de", "que", "en", "los", "para", "con", "una", "por"]),
    "en": frozenset(["the", "and", "is", "are", "with", "for", "your", "this", "that", "of"]),
    "eu": frozenset(
        ["da", "eta", "ez", "du", "dira", "dago", "egin", "bat", "zure", "zer", "eguraldi", "lursail"]
    ),
}
_WORD = re.compile(r"[^\W\d_]+")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def detect_language(text: str) -> str | None:
    """Best stopword match, or None when undetermined (tie / no hits)."""
    tokens = _WORD.findall(text.lower())
    scores = {lang: sum(t in words for t in tokens) for lang, words in _STOPWORDS.items()}
    best = max(scores.values())
    winners = [lang for lang, score in scores.items() if score == best]
    if best == 0 or len(winners) != 1:
        return None
    return winners[0]


def contains_citation(text: str, sources: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(s.lower() in lowered for s in sources) or bool(_ISO_DATE.search(text))


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

CompleteFn = Callable[..., Awaitable[LLMReply]]


class LiveProvider:
    """The module's real provider: nothing is patched, env decides the model."""

    def complete_for(self, case: Case) -> CompleteFn | None:
        return None


_TEXT = {
    "es": "Según {src} ({date}), la información de tu parcela es la que se indica para la consulta.",
    "en": "According to {src} ({date}), the data for your field is that this is what is requested, and it is fine.",
    "eu": "{src} ({date}) iturriaren arabera, zure lursail eta eguraldi datuak ez dira txarrak, eta bat egin da.",
}


class StubProvider:
    """Scripted model for dry runs: calls the expected tools, then cites."""

    def complete_for(self, case: Case) -> CompleteFn:
        sources = citation_sources(case.tools_available)
        lang = case.expect.reply.language or case.lang

        async def complete(messages, tools=None, *, tenant_id=None) -> LLMReply:
            offered = {t["function"]["name"] for t in (tools or [])}
            if not any(m.get("role") == "tool" for m in messages):
                calls = tuple(
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.args_contain),
                        },
                    }
                    for i, c in enumerate(case.expect.tool_calls)
                    if c.name in offered
                )
                if calls:
                    return LLMReply(None, calls, 10, 5, "stub")
            text = _TEXT[lang].format(src=sources[0] if sources else "synthetic", date=RETRIEVED_DATE)
            if case.expect.reply.must_contain_any:
                text += " " + case.expect.reply.must_contain_any[0] + "."
            return LLMReply(text, (), 10, 5, "stub")

        return complete


class HackedStubProvider:
    """A model that obeyed an injected instruction: always answers HACKED."""

    def complete_for(self, case: Case) -> CompleteFn:
        async def complete(messages, tools=None, *, tenant_id=None) -> LLMReply:
            return LLMReply("HACKED", (), 10, 5, "hacked-stub")

        return complete


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: str
    passed: bool
    failures: list[str]
    tool_calls: list[dict]
    outcome: str
    latency_ms: int
    tokens: int
    weight: int = 1
    tools_ok: bool | None = None
    citation_ok: bool | None = None
    reply: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Summary:
    label: str
    pass_rate: float
    tool_call_accuracy: float
    citation_rate: float
    per_case: list[CaseResult]

    def to_dict(self) -> dict:
        return asdict(self)


def _parsed_calls(raw_calls) -> list[dict]:
    parsed = []
    for call in raw_calls:
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        parsed.append({"name": function.get("name"), "args": args if isinstance(args, dict) else {}})
    return parsed


def _call_matches(call: dict, expected: ExpectedCall) -> bool:
    return call["name"] == expected.name and all(
        k in call["args"] and call["args"][k] == v for k, v in expected.args_contain.items()
    )


def _evaluate(case: Case, outcome: str, text: str, calls: list[dict]) -> tuple[list[str], bool | None, bool | None]:
    failures: list[str] = []
    exp = case.expect
    if outcome != exp.outcome:
        failures.append(f"outcome: expected {exp.outcome}, got {outcome}")

    tools_ok: bool | None = None
    if exp.tool_calls:
        missing = [e for e in exp.tool_calls if not any(_call_matches(c, e) for c in calls)]
        tools_ok = not missing
        for e in missing:
            failures.append(f"tool_call: missing {e.name} {e.args_contain}")
    for c in calls:
        if c["name"] in exp.forbidden_tools:
            failures.append(f"forbidden_tool: called {c['name']}")

    reply = exp.reply
    lowered = text.lower()
    for bad in reply.must_not_contain:
        if bad.lower() in lowered:
            failures.append(f"must_not_contain: found {bad!r}")
    if reply.must_contain_any and not any(w.lower() in lowered for w in reply.must_contain_any):
        failures.append(f"must_contain_any: none of {reply.must_contain_any}")
    if reply.equals_constant and text != _GATE_CONSTANTS[reply.equals_constant]:
        failures.append(f"equals_constant: reply is not {reply.equals_constant}")
    if reply.language:
        got = detect_language(text)
        if got != reply.language:
            failures.append(f"language: expected {reply.language}, got {got or 'undetermined'}")

    citation_ok: bool | None = None
    if reply.contains_citation is not None:
        has = contains_citation(text, citation_sources(case.tools_available))
        citation_ok = has == reply.contains_citation
        if not citation_ok:
            failures.append(f"citation: expected {reply.contains_citation}, found {has}")
    return failures, tools_ok, citation_ok


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

_OTHER_TENANT_KEY = ("other-tenant", "eval", "u-1")


def _context(case: Case) -> SessionContext:
    return SessionContext("synthetic", "user-1", ("Farmer",), "eval", "u-1", case.id)


async def run_case(case: Case, provider, *, label: str = "") -> CaseResult:
    ctx = _context(case)
    ctx_key = (ctx.tenant_id, ctx.channel, ctx.channel_user_id)
    store: dict[tuple, list[dict]] = {}
    seed = [m.model_dump() for m in case.memory]
    store[ctx_key if case.memory_owner == "self" else _OTHER_TENANT_KEY] = seed
    extra: dict[str, Any] = {"memory_load_keys": [], "memory_loaded_counts": []}

    async def fake_tools_for(tenant_id: str):
        return build_tools(case.tools_available, case.fixture_variant)

    async def fake_load(c: SessionContext, budget_chars: int) -> list[dict]:
        key = (c.tenant_id, c.channel, c.channel_user_id)
        loaded = [dict(m) for m in store.get(key, [])]
        extra["memory_load_keys"].append(key)
        extra["memory_loaded_counts"].append(len(loaded))
        return loaded

    async def fake_save(c: SessionContext, messages: list[dict], budget_chars: int) -> None:
        key = (c.tenant_id, c.channel, c.channel_user_id)
        store.setdefault(key, []).extend(messages)

    started = time.monotonic()
    try:
        with ExitStack() as stack:
            stack.enter_context(patch("app.agent.registry.tools_for", new=fake_tools_for))
            stack.enter_context(patch("app.agent.memory.load", new=fake_load))
            stack.enter_context(patch("app.agent.memory.save", new=fake_save))
            complete = provider.complete_for(case)
            if complete is not None:
                stack.enter_context(patch("app.agent.loop.complete", new=complete))

            if case.kind == "text":
                result = await run_turn(case.input, case.turn_budget(), ctx)
                text, outcome = result.text, result.outcome
                raw_calls = result.tool_calls
                tokens = result.tokens_prompt + result.tokens_completion
            else:
                text, outcome, raw_calls, tokens = await _run_gate(case, ctx, stack, extra)
    except Exception as exc:  # noqa: BLE001 - a harness fault is a failed case, not a crashed run
        return CaseResult(
            case.id, False, [f"harness_error: {type(exc).__name__}"], [], "harness_error",
            int((time.monotonic() - started) * 1000), 0, case.weight, extra=extra,
        )

    calls = _parsed_calls(raw_calls)
    failures, tools_ok, citation_ok = _evaluate(case, outcome, text, calls)
    if case.kind != "text":
        failures += _gate_failures(extra)
    extra["reply"] = text
    return CaseResult(
        id=case.id,
        passed=not failures,
        failures=failures,
        tool_calls=calls,
        outcome=outcome,
        latency_ms=int((time.monotonic() - started) * 1000),
        tokens=tokens,
        weight=case.weight,
        tools_ok=tools_ok,
        citation_ok=citation_ok,
        reply=text,
        extra=extra,
    )


async def _run_gate(case: Case, ctx: SessionContext, stack: ExitStack, extra: dict):
    """Drive handle_message for a voice / empty message; count what it must not touch."""
    extra.update(run_turn_calls=0, check_and_count_calls=0, audit_outcomes=[])

    async def resolve_session(channel, channel_user_id, trace_id):
        return ctx

    async def record_turn(session, text, turn_result, latency_ms, trace_id):
        extra["audit_outcomes"].append(turn_result.outcome)

    async def check_and_count(*args, **kwargs):
        extra["check_and_count_calls"] += 1

    async def counted_run_turn(*args, **kwargs):
        extra["run_turn_calls"] += 1
        raise AssertionError("run_turn must not be reached by a gated message")

    stack.enter_context(patch("app.handlers.service.resolve_session", new=resolve_session))
    stack.enter_context(patch("app.handlers.record_turn", new=record_turn))
    stack.enter_context(patch("app.handlers.check_and_count", new=check_and_count))
    stack.enter_context(patch("app.handlers.run_turn", new=counted_run_turn))

    msg = InboundMessage(
        channel=ctx.channel,
        channel_user_id=ctx.channel_user_id,
        conversation_id="conv-1",
        text=None,
        voice=VoiceRef("synthetic-file", 5, "audio/ogg") if case.kind == "voice" else None,
        idempotency_key=f"eval-{case.id}",
        received_at=datetime.now(timezone.utc),
    )
    out = await handlers.handle_message(msg, trace_id=case.id)
    outcome = extra["audit_outcomes"][0] if extra["audit_outcomes"] else "none"
    return out.text, outcome, (), 0


def _gate_failures(extra: dict) -> list[str]:
    failures = []
    if extra["run_turn_calls"]:
        failures.append("gate: run_turn was reached")
    if extra["check_and_count_calls"]:
        failures.append("gate: quota was consumed")
    return failures


async def run_all(cases: list[Case], provider, label: str) -> Summary:
    results = [await run_case(c, provider, label=label) for c in cases]
    total_weight = sum(r.weight for r in results) or 1
    passed_weight = sum(r.weight for r in results if r.passed)

    def rate(flags: list[bool | None]) -> float:
        relevant = [f for f in flags if f is not None]
        return sum(relevant) / len(relevant) if relevant else 1.0

    return Summary(
        label=label,
        pass_rate=passed_weight / total_weight,
        tool_call_accuracy=rate([r.tools_ok for r in results]),
        citation_rate=rate([r.citation_ok for r in results]),
        per_case=results,
    )
