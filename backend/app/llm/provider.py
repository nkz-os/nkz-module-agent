"""The single door to the language model provider.

Every deployment of this platform chooses its own model, so nothing here
names one. LiteLLM normalises tool-calling across providers, which is why
this module depends on it rather than on a single vendor's client — and why
no other file may import it: swapping the library, or swapping providers,
must stay a change to this one file.

Two failure modes matter to every caller, and both are collapsed here so
nobody downstream has to know litellm exists:

- No model configured (a fresh install, before an operator sets one) is the
  normal state, not an error condition to crash on. `is_configured()` lets a
  caller check first; `complete()` raises the typed `LLMUnconfigured` instead
  of guessing a provider from an empty model string.
- Any failure once a call is attempted (timeout, rejected credentials, a
  response shaped differently than expected) is re-raised as
  `LLMProviderError`. The message is stripped of the configured API key
  before it is logged or attached to the exception, and `from None` stops
  Python's own traceback machinery from printing the original — an
  unredacted vendor exception chained onto ours would defeat the point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from litellm import acompletion

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMUnconfigured(Exception):
    """Raised by complete() when no model is configured for this deployment."""


class LLMProviderError(Exception):
    """Raised when a configured provider call fails, for any reason.

    The message is sanitized: the configured API key never appears in it.
    """


@dataclass(frozen=True)
class LLMReply:
    text: str | None
    tool_calls: tuple[dict, ...]
    tokens_prompt: int
    tokens_completion: int
    model: str


def is_configured() -> bool:
    """Whether an operator has set a model for this deployment."""
    return bool(get_settings().llm_model)


def _sanitize(message: str, secret: str) -> str:
    """Strip a configured secret out of a message before it can leak.

    A provider's own error text can echo back the credential it just
    rejected (some auth-failure responses do this, in whole or in part), so
    scrubbing the literal secret value — not just avoiding logging it
    ourselves — is what actually closes the leak.
    """
    if not secret:
        return message
    return message.replace(secret, "[redacted]")


async def complete(
    messages: list[dict], tools: list[dict] | None = None
) -> LLMReply:
    """Send one completion request to the configured provider.

    Raises LLMUnconfigured if no model is set (no provider is contacted in
    that case), or LLMProviderError if the configured provider call fails.
    """
    settings = get_settings()
    if not settings.llm_model:
        raise LLMUnconfigured("LLM_MODEL is not set for this deployment")

    kwargs: dict = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": settings.llm_temperature,
        "timeout": settings.turn_timeout_seconds,
    }
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key
    if tools:
        kwargs["tools"] = tools

    try:
        response = await acompletion(**kwargs)
        choice = response.choices[0].message
        raw_calls = getattr(choice, "tool_calls", None) or ()
        usage = getattr(response, "usage", None)
        return LLMReply(
            text=getattr(choice, "content", None),
            tool_calls=tuple(
                c if isinstance(c, dict) else c.model_dump() for c in raw_calls
            ),
            tokens_prompt=getattr(usage, "prompt_tokens", 0) or 0,
            tokens_completion=getattr(usage, "completion_tokens", 0) or 0,
            model=settings.llm_model,
        )
    except Exception as exc:
        detail = _sanitize(str(exc), settings.llm_api_key)
        logger.error(
            "llm_completion_failed model=%s error=%s", settings.llm_model, detail
        )
        # `from None`: suppress __context__ so nothing prints the original
        # (possibly unredacted) exception via traceback chaining.
        raise LLMProviderError(detail) from None
