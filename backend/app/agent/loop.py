"""One conversational turn, bounded.

Ask the model once. If it answers, that is the turn. If it asks for tools
instead, execute the declared ones and return the results to the model for a
final answer, inside the same bounded loop. The per-iteration budget checks
live inside a loop shape because a tool round-trip is a second model call --
and a loop nobody remembers to bound is unbounded spend.

Nothing raises out of here. The caller is a background task with no HTTP
client left to receive an error, so an escaping exception is a turn lost in
silence. Every failure becomes one of the outcomes the audit table's CHECK
constraint accepts, and a message the farmer can act on -- never the
provider's own error text, a stack trace, or anything else internal to this
deployment.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from pydantic import ValidationError

from app.agent import memory, registry
from app.agent.budget import BudgetExhausted, TurnBudget
from app.agent.prompt import MAX_PAYLOAD as _FENCE_MAX_CHARS
from app.agent.prompt import system_prompt, wrap_untrusted
from app.domain.session import SessionContext
from app.llm.provider import LLMUnconfigured, complete
from app.tools.catalog import ToolDefinition

logger = logging.getLogger(__name__)

# Label attached to the farmer's message when it is fenced for the model
# (see wrap_untrusted). Model-facing only, never shown to a person.
_USER_MESSAGE_SOURCE = "user message"

# Backend strings are neutral English, matching app.handlers -- there is no
# i18n path for user-facing text anywhere in this module yet (the system
# prompt is model-facing only, per app.agent.prompt). If one is added later,
# these four constants are what needs to move behind it.
UNCONFIGURED_TEXT = (
    "This assistant has no language model configured in this deployment. "
    "An administrator needs to set one before it can answer."
)
BUDGET_EXHAUSTED_TEXT = (
    "That took longer, or asked for more, than I am allowed to spend on one "
    "question. Try asking something narrower."
)
ERROR_TEXT = (
    "Something went wrong answering that. It has been logged; please try again."
)
# ASSUMPTION: exists for the degenerate provider reply -- text=None with no
# tool calls (some providers emit an empty content part). The turn is still
# answered, never with silence.
EMPTY_REPLY_TEXT = "I don't have an answer for that. Could you rephrase the question?"

# Memory is bounded by the turn's own token budget so it can shrink the
# prompt but never grow it. ~4 chars per token is a conservative ceiling for
# natural language plus JSON structure; the exact figure only needs to be
# order-of-magnitude sane, because memory is trimmed to fit whatever budget
# the turn already allows.
_CHARS_PER_TOKEN = 4


# Tokens kept free for the model's FINAL answer, after everything the second
# pass sends. A real answer in the farmer's language is a few hundred tokens;
# a smaller reserve ends otherwise-complete turns as budget_exhausted.
_REPLY_RESERVE_TOKENS = 300
# Expected size of the first pass's own output (a tool call), used only to
# size memory before any provider call has happened.
_FIRST_PASS_COMPLETION_TOKENS = 100
# Of what is left after the fixed overhead, the share the tool result is
# expected to use (the rest goes to memory, which is sent on every pass).
_TOOL_RESULT_SHARE = 0.6
# Structure around one tool result that its serialized text does not include:
# the role/tool_call_id envelope, the fence header and closing marker.
_TOOL_MESSAGE_OVERHEAD_CHARS = 150
# Below this a tool result is replaced by a compact "omitted" notice.
_MIN_TOOL_RESULT_CHARS = 200


def _json_tokens(obj: object) -> int:
    """Prompt tokens of ``obj`` as it travels: its JSON form, structure included."""
    return len(json.dumps(obj)) // _CHARS_PER_TOKEN


def _size_memory(
    budget: TurnBudget, system_message: dict, specs: list[dict] | None,
    user_message: dict,
) -> int:
    """Characters of memory the first pass may carry.

    Charging happens AFTER each provider call, so whatever is put in a prompt
    is billed even when the turn is then discarded as budget_exhausted. A turn
    that may use tools is TWO passes, and each re-sends the system prompt, the
    tool specs, the memory and the user message (all counted as their JSON
    form, structure included). Before pass 1 only an estimate is possible:

        left   = max_tokens - passes * fixed - first-pass output - final reserve
        memory = (1 - tool share) * left / passes

    The tool result is NOT sized here: it is sized after pass 1 from the
    provider's real token count (see _tool_result_chars), so an optimistic
    memory estimate shrinks the tool result instead of ending the turn.
    """
    passes = 2 if specs else 1
    fixed = _json_tokens(system_message) + _json_tokens(user_message)
    if specs:
        fixed += _json_tokens(specs)
    left = budget.max_tokens - passes * fixed - _REPLY_RESERVE_TOKENS
    if specs:
        left -= _FIRST_PASS_COMPLETION_TOKENS
        left = int(left * (1 - _TOOL_RESULT_SHARE)) // passes
    return max(left, 0) * _CHARS_PER_TOKEN


def _tool_result_chars(
    budget: TurnBudget, reply: object, assistant_message: dict, n_calls: int
) -> int:
    """Characters each tool result of this pass may use.

    The next pass re-sends the prompt that has just been billed (the
    provider's real ``tokens_prompt``) plus the assistant tool-call message,
    and then the results; the final answer needs ``_REPLY_RESERVE_TOKENS``
    after that. What is left, in characters, is shared by the calls of the
    pass. 0 means "no room": the call gets a compact omitted notice.

    NOTE: the ruling's literal formula (max - prompt - completion - reserve)
    omits that the second pass pays for the prompt again; this one includes it.
    """
    remaining = (
        budget.max_tokens
        - budget.spent_tokens
        - reply.tokens_prompt
        - _json_tokens(assistant_message)
        - _REPLY_RESERVE_TOKENS
    )
    per_call = (
        max(remaining, 0) * _CHARS_PER_TOKEN // n_calls - _TOOL_MESSAGE_OVERHEAD_CHARS
    )
    return max(per_call, 0)


async def _load_memory(ctx: SessionContext, budget_chars: int) -> list[dict]:
    """Load prior context; a failure degrades to no memory, never a lost turn."""
    try:
        return await memory.load(ctx, budget_chars)
    except Exception:
        logger.warning(
            "memory_load_failed tenant_id=%s channel=%s",
            ctx.tenant_id, ctx.channel, exc_info=True,
        )
        return []


async def _save_memory(
    ctx: SessionContext, messages: list[dict], budget_chars: int
) -> None:
    """Persist the turn's essentials (appended, trimmed); never blocks the reply."""
    try:
        await memory.save(ctx, messages, budget_chars)
    except Exception:
        logger.warning(
            "memory_save_failed tenant_id=%s channel=%s",
            ctx.tenant_id, ctx.channel, exc_info=True,
        )


@dataclass(frozen=True)
class TurnResult:
    text: str
    outcome: str
    model: str | None
    tool_calls: tuple[dict, ...]
    tokens_prompt: int
    tokens_completion: int


async def run_turn(
    user_text: str, budget: TurnBudget, ctx: SessionContext
) -> TurnResult:
    """Turn one farmer message into one reply, inside `budget`.

    outcome is one of: ok, budget_exhausted, unconfigured, error -- a subset
    of the values the agent_turn_audit CHECK accepts (the voice gate adds
    'refused').
    """
    budget.start()
    model: str | None = None
    prompt_tokens = 0
    completion_tokens = 0
    seen_calls: list[dict] = []

    # The whole setup lives inside the turn's try: "nothing raises out of
    # here" is a structural invariant, not a happy-path hope. A broken
    # adapter import or a schema failure during tool resolution must become
    # an audited error turn, never a lost one.
    try:
        tool_defs = await registry.tools_for(ctx.tenant_id)
        tools_by_name = {tool.name: tool for tool in tool_defs}
        # The farmer's text is content the agent did not author -- it is
        # fenced, never concatenated raw, so nothing in it can be read as an
        # instruction to the model. See app.agent.prompt.wrap_untrusted.
        user_message = {
            "role": "user",
            "content": wrap_untrusted(_USER_MESSAGE_SOURCE, user_text),
        }
        tools = [registry.openai_spec(tool) for tool in tool_defs] or None
        system_text = system_prompt(bool(tool_defs))
        system_message = {"role": "system", "content": system_text}
        memory_chars = _size_memory(budget, system_message, tools, user_message)
        messages: list[dict] = [
            system_message,
            *await _load_memory(ctx, memory_chars),
            user_message,
        ]

        while True:
            budget.check_deadline()
            budget.spend_iteration()

            reply = await complete(messages, tools=tools, tenant_id=ctx.tenant_id)
            model = reply.model
            prompt_tokens += reply.tokens_prompt
            completion_tokens += reply.tokens_completion
            budget.spend_tokens(reply.tokens_prompt, reply.tokens_completion)

            if not reply.tool_calls:
                final_text = reply.text or EMPTY_REPLY_TEXT
                await _save_memory(
                    ctx,
                    [
                        user_message,
                        {"role": "assistant", "content": final_text},
                    ],
                    memory_chars,
                )
                return TurnResult(
                    text=final_text,
                    outcome="ok",
                    model=model,
                    tool_calls=tuple(seen_calls),
                    tokens_prompt=prompt_tokens,
                    tokens_completion=completion_tokens,
                )

            # The model asked for tools: record its assistant message (with
            # the tool_calls it made) so the tool results that follow have a
            # preceding turn to answer, then execute each call. A call whose
            # name is not in the declared set is answered "unknown tool" and
            # the turn continues -- it is never executed, and never answered
            # with an invented result.
            assistant_message = {
                "role": "assistant",
                "content": reply.text,
                "tool_calls": list(reply.tool_calls),
            }
            messages.append(assistant_message)
            # One pass can ask for several tools; they share what is left.
            per_call_chars = _tool_result_chars(
                budget, reply, assistant_message, len(reply.tool_calls)
            )
            for call in reply.tool_calls:
                seen_calls.append(call)
                budget.spend_tool_call()
                messages.append(
                    await _dispatch_call(
                        call, tools_by_name, ctx, per_call_chars, budget
                    )
                )

    except LLMUnconfigured:
        logger.critical("llm_unconfigured -- no model set for this deployment")
        return TurnResult(
            text=UNCONFIGURED_TEXT,
            outcome="unconfigured",
            model=None,
            tool_calls=(),
            tokens_prompt=0,
            tokens_completion=0,
        )
    except BudgetExhausted as exc:
        logger.warning("turn_budget_exhausted reason=%s", exc.reason)
        return TurnResult(
            text=BUDGET_EXHAUSTED_TEXT,
            outcome="budget_exhausted",
            model=model,
            tool_calls=tuple(seen_calls),
            tokens_prompt=prompt_tokens,
            tokens_completion=completion_tokens,
        )
    except Exception:
        # Anything else: a provider failure (LLMProviderError, already
        # sanitized by the facade) or a genuinely unexpected bug. Neither
        # gets its own outcome value -- the audit CHECK constraint has none
        # -- and neither gets its message anywhere near the farmer: only the
        # fixed ERROR_TEXT is returned, the exception itself is logged.
        logger.exception("turn_failed_in_agent_loop")
        return TurnResult(
            text=ERROR_TEXT,
            outcome="error",
            model=model,
            tool_calls=tuple(seen_calls),
            tokens_prompt=prompt_tokens,
            tokens_completion=completion_tokens,
        )


async def _dispatch_call(
    call: dict,
    tools_by_name: dict[str, ToolDefinition],
    ctx: SessionContext,
    max_chars: int = _FENCE_MAX_CHARS,
    budget: TurnBudget | None = None,
) -> dict:
    """Execute one model tool call into a ``role: tool`` result message.

    The provider facade (LiteLLM) expects each tool result to carry the
    ``tool_call_id`` of the call it answers. A name outside the declared set
    is answered "unknown tool" without execution; malformed arguments are
    answered "invalid arguments" without execution.
    """
    function = call.get("function")
    if not isinstance(function, dict):
        function = {}
    name = function.get("name")
    arguments = function.get("arguments") or "{}"
    call_id = call.get("id") or ""
    label = _tool_label(name)

    # The name comes from the model: anything but a string (a dict, a list) is
    # an unknown tool, not a TypeError on the dict lookup.
    tool = tools_by_name.get(name) if isinstance(name, str) and name else None
    if tool is None:
        return _tool_result(call_id, _fence_tool(label, "unknown tool"))

    if not isinstance(arguments, str):
        return _tool_result(call_id, _fence_tool(label, "invalid arguments"))
    try:
        args = tool.args_model.model_validate_json(arguments)
    except ValidationError:
        return _tool_result(call_id, _fence_tool(label, "invalid arguments"))

    # One tool must neither hold the turn past its deadline nor end it: a slow
    # or failing adapter becomes one "unavailable" result and the turn goes on.
    timeout = _TOOL_TIMEOUT_SECONDS
    if budget is not None:
        timeout = min(timeout, budget.remaining_seconds())
    try:
        result = await asyncio.wait_for(tool.handler(ctx, args), timeout=timeout)
        # Reading the result is inside the isolation too: a handler that
        # returns something that is not a ToolResult is a tool error, not a
        # lost turn.
        if result.ok:
            if max_chars < _MIN_TOOL_RESULT_CHARS:
                logger.warning(
                    "tool_result_omitted tenant_id=%s tool=%s room_chars=%d",
                    ctx.tenant_id, tool.name, max_chars,
                )
            body = _serialize_payload(result.payload, result.citations, max_chars)
        else:
            # The reason is the adapter's own, never a fabricated payload.
            body = f"unavailable: {result.unavailable_reason or ''}"
    except asyncio.TimeoutError:
        logger.warning(
            "tool_timeout tenant_id=%s tool=%s timeout=%.1f",
            ctx.tenant_id, tool.name, timeout,
        )
        return _tool_result(call_id, _fence_tool(label, "unavailable: tool timed out"))
    except Exception:
        logger.warning(
            "tool_failed tenant_id=%s tool=%s", ctx.tenant_id, tool.name,
            exc_info=True,
        )
        return _tool_result(call_id, _fence_tool(label, "unavailable: tool error"))

    return _tool_result(call_id, _fence_tool(label, body))


# The fence (app.agent.prompt.wrap_untrusted) caps its body at 8000 chars, so
# that is the effective ceiling whatever the turn's arithmetic (_tool_result_chars)
# would allow: the per-turn cap is min(budget-derived, 8000).
_TRUNCATED_MARK = " …[truncated]"

# Upper bound for one tool handler (design: tool timeouts <= 8 s); the turn's
# remaining time can only shorten it.
_TOOL_TIMEOUT_SECONDS = 8.0


def _tool_label(name: object) -> str:
    """A safe, single-line label for the fence header (the name is model-supplied)."""
    text = name if isinstance(name, str) else "?"
    return "tool " + "".join(ch if ch.isprintable() else " " for ch in text)[:64]


def _fence_tool(label: str, body: str) -> str:
    """Everything the agent did not write reaches the model inside the fence."""
    return wrap_untrusted(label, body)


def _escaped_len(text: str) -> int:
    """Length of ``text`` as a JSON string value (its quotes become ``\\"``).

    The prompt is billed as the messages' JSON, where this text sits escaped
    inside a string; JSON-in-JSON is noticeably longer than the raw text.
    """
    return len(json.dumps(text)) - 2


def _cut_to(text: str, cap: int) -> str:
    """``text`` cut (with a marker) until its escaped length fits ``cap``."""
    keep = max(cap - len(_TRUNCATED_MARK), 0)
    out = text[:keep]
    while keep > 0 and _escaped_len(out) + len(_TRUNCATED_MARK) > cap:
        keep -= max((_escaped_len(out) + len(_TRUNCATED_MARK) - cap) // 2, 1)
        out = text[: max(keep, 0)]
    return out + _TRUNCATED_MARK


def _citation_dicts(citations: tuple) -> list[dict]:
    return [
        {
            "source": c.source,
            "retrieved_at": c.retrieved_at.isoformat(),
            "detail": c.detail,
        }
        for c in citations
    ]


def _trim_plan(payload: object):
    """How to shrink ``payload``: (items, rebuild) or None when it cannot be trimmed.

    A list payload is trimmed directly. A dict is trimmed through its longest
    list value (points, parcels, alerts, products, trials, ...); every other
    key (summary, totals, dates) is a fixed part and comes first.
    """
    if isinstance(payload, list):
        return payload, lambda sub: sub
    if isinstance(payload, dict):
        lists = {k: v for k, v in payload.items() if isinstance(v, list)}
        if lists:
            key = max(lists, key=lambda k: len(lists[k]))
            fixed = {k: v for k, v in payload.items() if k != key}
            return lists[key], lambda sub: {**fixed, key: sub}
    return None


def _serialize_payload(
    payload: object, citations: tuple = (), max_chars: int = _FENCE_MAX_CHARS
) -> str:
    """Serialize citations + data so the result fits ``max_chars`` and stays valid.

    Fixed parts come first (citations, then ``omitted`` when items were
    dropped, then the data with its own non-list keys ahead of the list). The
    cap is met by dropping list items from the end, never by cutting text, so
    citations and summaries always survive. Only a payload with no list is
    text-cut, with a marker. With less room than _MIN_TOOL_RESULT_CHARS, or
    when not even the fixed parts fit, the data is replaced by a compact
    ``{"omitted": n, "citations": [...]}``. Fencing (which defuses markers)
    happens after, on the finished text.
    """
    cites = _citation_dicts(citations)
    plan = _trim_plan(payload)
    n_items = len(plan[0]) if plan else 1

    def compact() -> str:
        return json.dumps({"omitted": n_items, "citations": cites}, default=str)

    if max_chars < _MIN_TOOL_RESULT_CHARS:
        return compact()
    cap = min(max_chars, _FENCE_MAX_CHARS)

    def render(m: int) -> str:
        assert plan is not None
        items, rebuild = plan
        body: dict = {"citations": cites}
        if m < len(items):
            body["omitted"] = len(items) - m
        body["data"] = rebuild(items[:m])
        return json.dumps(body, default=str)

    if plan is None:
        text = json.dumps({"citations": cites, "data": payload}, default=str)
        return _cut_to(text, cap) if _escaped_len(text) > cap else text

    if _escaped_len(render(len(plan[0]))) <= cap:
        return render(len(plan[0]))
    if _escaped_len(render(0)) > cap:
        return compact()
    low, high = 0, len(plan[0]) - 1  # render(low) fits, render(len) does not
    while low < high:
        mid = (low + high + 1) // 2
        if _escaped_len(render(mid)) <= cap:
            low = mid
        else:
            high = mid - 1
    return render(low)


def _tool_result(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}
