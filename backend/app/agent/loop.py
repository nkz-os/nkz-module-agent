"""One conversational turn, bounded.

Ask the model once. If it answers, that is the turn. If it asks for a tool
instead, the answer is a refusal, not a return trip to the model: the
registry is empty in this phase (see app.agent.registry), so no tool
declarations were ever sent, and asking again could only reproduce the same
request from the model at a second cost. The per-iteration budget checks
stay inside a loop shape anyway, because that is the shape a later phase's
tool dispatch reuses -- a loop retrofitted onto a straight-line function
later is a loop nobody remembers to bound. In this phase the loop body
always returns on its first pass.

Nothing raises out of here. The caller is a background task with no HTTP
client left to receive an error, so an escaping exception is a turn lost in
silence. Every failure becomes one of the outcomes the audit table's CHECK
constraint accepts, and a message the farmer can act on -- never the
provider's own error text, a stack trace, or anything else internal to this
deployment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agent import registry
from app.agent.budget import BudgetExhausted, TurnBudget
from app.agent.prompt import system_prompt, wrap_untrusted
from app.llm.provider import LLMUnconfigured, complete

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
REFUSED_TEXT = (
    "I cannot look up platform data yet -- that capability is not available "
    "in this version. I will not guess about a real farm."
)
EMPTY_REPLY_TEXT = "I don't have an answer for that. Could you rephrase the question?"


@dataclass(frozen=True)
class TurnResult:
    text: str
    outcome: str
    model: str | None
    tool_calls: tuple[dict, ...]
    tokens_prompt: int
    tokens_completion: int


async def run_turn(user_text: str, budget: TurnBudget) -> TurnResult:
    """Turn one farmer message into one reply, inside `budget`.

    outcome is one of: ok, refused, budget_exhausted, unconfigured, error --
    exactly the values the agent_turn_audit CHECK constraint accepts.
    """
    budget.start()
    messages: list[dict] = [
        {"role": "system", "content": system_prompt(registry.has_tools())},
        # The farmer's text is content the agent did not author -- it is
        # fenced, never concatenated raw, so nothing in it can be read as an
        # instruction to the model. See app.agent.prompt.wrap_untrusted.
        {"role": "user", "content": wrap_untrusted(_USER_MESSAGE_SOURCE, user_text)},
    ]
    tools = registry.tool_specs() or None
    model: str | None = None
    prompt_tokens = 0
    completion_tokens = 0
    seen_calls: list[dict] = []

    try:
        while True:
            budget.check_deadline()
            budget.spend_iteration()

            reply = await complete(messages, tools=tools)
            model = reply.model
            prompt_tokens += reply.tokens_prompt
            completion_tokens += reply.tokens_completion
            budget.spend_tokens(reply.tokens_prompt, reply.tokens_completion)

            if not reply.tool_calls:
                return TurnResult(
                    text=reply.text or EMPTY_REPLY_TEXT,
                    outcome="ok",
                    model=model,
                    tool_calls=tuple(seen_calls),
                    tokens_prompt=prompt_tokens,
                    tokens_completion=completion_tokens,
                )

            # The model asked for a tool though none was ever declared to it
            # (tools is None above -- the registry is empty this phase).
            # Charge the attempt before refusing: a reply crammed with more
            # calls than this deployment allows should end the turn on that
            # limit, not get a free pass just because none of them could
            # ever be run. When a later phase gives the registry real tools,
            # dispatching them replaces this refusal.
            for call in reply.tool_calls:
                seen_calls.append(call)
                budget.spend_tool_call()

            logger.warning(
                "model_requested_tool_with_empty_registry count=%d",
                len(reply.tool_calls),
            )
            return TurnResult(
                text=REFUSED_TEXT,
                outcome="refused",
                model=model,
                tool_calls=tuple(seen_calls),
                tokens_prompt=prompt_tokens,
                tokens_completion=completion_tokens,
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
