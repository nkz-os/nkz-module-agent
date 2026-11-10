"""The single door to the language model provider.

Every deployment of this platform chooses its own model, so nothing here
names one. Which provider serves a tenant is decided in `resolver.py`; this
module only consumes that decision. LiteLLM normalises tool-calling across providers, which is why
this module depends on it rather than on a single vendor's client — and why
no other file may import it: swapping the library, or swapping providers,
must stay a change to this one file.

Two failure modes matter to every caller, and both are collapsed here so
nobody downstream has to know litellm exists:

- No model resolved (a fresh install, before an operator sets one) is the
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

import base64
import logging
import re
import urllib.parse
from dataclasses import dataclass

import httpx
import litellm
from litellm import acompletion

from app.config import get_settings
from app.llm.resolver import resolve_provider

logger = logging.getLogger(__name__)


class LLMUnconfigured(Exception):
    """Raised by complete() when no provider resolves for the tenant.

    `notice` says why a tenant's own row did not resolve ("blocked",
    "key_unreadable"); None means there was nothing configured.
    """

    def __init__(self, message: str = "", notice: str | None = None):
        super().__init__(message)
        self.notice = notice


class LLMProviderError(Exception):
    """Raised when a configured provider call fails, for any reason.

    The message is sanitized: the configured API key never appears in it.
    `origin` says whose provider failed (tenant/platform) for the audit.
    `kind` is a fixed code for the failure class; `detail` is the sanitized,
    length-capped provider text. Only the connection-test route may show
    either to a person; agent turns never do.
    """

    def __init__(
        self,
        message: str = "",
        origin: str | None = None,
        kind: str = "provider_error",
        detail: str | None = None,
    ):
        super().__init__(message)
        self.origin = origin
        self.kind = kind
        self.detail = message if detail is None else detail


@dataclass(frozen=True)
class LLMReply:
    text: str | None
    tool_calls: tuple[dict, ...]
    tokens_prompt: int
    tokens_completion: int
    model: str
    origin: str | None = None


async def is_configured(tenant_id: str | None) -> bool:
    """Whether a provider resolves for this tenant (tenant row, or platform
    when the fallback is enabled)."""
    return (await resolve_provider(tenant_id)).origin != "none"


_DETAIL_MAX = 300
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NOISE_LINES = ("give feedback / get help", "litellm.info")


_MIN_FRAGMENT = 8


def _secret_forms(secret: str) -> list[tuple[str, int]]:
    """(form, shortest fragment of it that counts as a leak) for one key."""
    raw = secret.encode()
    forms = {
        secret,
        urllib.parse.quote(secret, safe=""),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
        base64.b64encode(raw).decode().rstrip("="),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
    }
    out = [(f, min(_MIN_FRAGMENT, len(f))) for f in forms if f]
    if len(secret) >= _MIN_FRAGMENT:
        out.append((secret[:-4], len(secret) - 4))
    return out


def _mark_leaks(text: str, forms: list[tuple[str, int]]) -> list[bool]:
    """Flag every char of `text` inside a run that is a contiguous piece of
    some form with at least its minimum length (longest-common-substring DP,
    O(len(text) * len(form)))."""
    flags = [False] * len(text)
    for form, minlen in forms:
        prev = [0] * (len(form) + 1)
        for i, ch in enumerate(text):
            cur = [0] * (len(form) + 1)
            for k, fc in enumerate(form):
                if ch == fc:
                    run = prev[k] + 1
                    cur[k + 1] = run
                    if run >= minlen:
                        for q in range(i - minlen + 1, i + 1):
                            flags[q] = True
            prev = cur
    return flags


def _sanitize(message: str, secret: str) -> str:
    """Make a provider message safe to show: no key, no library noise.

    A provider's own error text can echo back the credential it just
    rejected: whole, URL-quoted, base64, cut short at either end, or split
    by a line break. ANSI codes, LiteLLM's "Give Feedback" / "LiteLLM.Info"
    lines and whitespace runs are normalised first; then matching runs on the
    text with whitespace ignored, and the redaction covers the original span.
    """
    lines = [
        ln for ln in _ANSI.sub("", message).splitlines()
        if not any(n in ln.lower() for n in _NOISE_LINES)
    ]
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not secret or not text:
        return text
    kept = [i for i, ch in enumerate(text) if not ch.isspace()]
    flags = _mark_leaks("".join(text[i] for i in kept), _secret_forms(secret))
    hit = [False] * len(text)
    for pos, f in zip(kept, flags):
        hit[pos] = f
    # Whitespace between two flagged chars belongs to a split key.
    for i, ch in enumerate(text):
        if ch.isspace() and 0 < i < len(text) - 1 and hit[i - 1] and hit[i + 1]:
            hit[i] = True
    out: list[str] = []
    for i, ch in enumerate(text):
        if hit[i]:
            if not out or out[-1] != "[redacted]":
                out.append("[redacted]")
        else:
            out.append(ch)
    return "".join(out)


def _is_connection_failure(exc: Exception) -> bool:
    """LiteLLM reports a refused/failed connect as InternalServerError for
    some providers; a genuine provider 500 must stay a provider error."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (httpx.ConnectError, httpx.ConnectTimeout)):
            return True
        if type(cur).__module__.startswith("aiohttp") and "Connect" in type(cur).__name__:
            return True
        cur = cur.__cause__ or cur.__context__
    return "connection error" in str(exc).lower()


def _kind_of(exc: Exception) -> str:
    """Fixed failure code for an exception raised by the provider call."""
    if isinstance(exc, (litellm.AuthenticationError, litellm.PermissionDeniedError)):
        return "auth_failed"
    if isinstance(exc, litellm.NotFoundError):
        return "not_found"
    if isinstance(exc, litellm.RateLimitError):
        return "quota_or_rate_limit"
    if isinstance(exc, litellm.BadRequestError):
        return "bad_request"
    if isinstance(exc, (litellm.Timeout, litellm.APIConnectionError)):
        return "unreachable"
    if isinstance(exc, litellm.InternalServerError) and _is_connection_failure(exc):
        return "unreachable"
    return "provider_error"


async def complete(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    tenant_id: str | None = None,
    max_tokens: int | None = None,
    timeout_s: float | None = None,
) -> LLMReply:
    """Send one completion request to the provider resolved for the tenant.

    Raises LLMUnconfigured if no provider resolves (no provider is contacted
    in that case), or LLMProviderError if the provider call fails.
    """
    settings = get_settings()
    resolved = await resolve_provider(tenant_id)
    if resolved.origin == "none":
        raise LLMUnconfigured(
            "no LLM provider resolved for this tenant", notice=resolved.notice
        )

    kwargs: dict = {
        "model": resolved.model,
        "messages": messages,
        "temperature": settings.llm_temperature,
        # Never longer than what the turn has left: a late call must not
        # outlive the turn and lose its (already paid) answer to staleness.
        "timeout": (
            settings.turn_timeout_seconds
            if timeout_s is None
            else max(0.001, min(settings.turn_timeout_seconds, timeout_s))
        ),
    }
    if resolved.api_base:
        kwargs["api_base"] = resolved.api_base
    if resolved.api_key:
        kwargs["api_key"] = resolved.api_key
    if resolved.origin == "tenant":
        kwargs["custom_llm_provider"] = resolved.provider
    if tools:
        kwargs["tools"] = tools
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

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
            model=resolved.model,
            origin=resolved.origin,
        )
    except Exception as exc:
        detail = _sanitize(str(exc), resolved.api_key)[:_DETAIL_MAX]
        logger.error(
            "llm_completion_failed model=%s origin=%s error=%s",
            resolved.model,
            resolved.origin,
            detail,
        )
        # `from None`: suppress __context__ so nothing prints the original
        # (possibly unredacted) exception via traceback chaining.
        raise LLMProviderError(
            detail, origin=resolved.origin, kind=_kind_of(exc), detail=detail
        ) from None


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """USD estimate from LiteLLM's price table; None when it cannot say
    (unknown model, any error)."""
    try:
        import litellm

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return float(prompt_cost) + float(completion_cost)
    except Exception:
        return None
