import ast
import logging

import pytest

from app.config import get_settings
from app.llm.provider import LLMProviderError, LLMUnconfigured, complete, is_configured


@pytest.fixture(autouse=True)
def clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _platform_fallback_on(monkeypatch):
    """Most tests here exercise the platform branch; BYOK ones override."""
    monkeypatch.setenv("LLM_PLATFORM_FALLBACK", "true")


@pytest.mark.asyncio
async def test_unconfigured_when_no_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "")
    assert await is_configured("t") is False


@pytest.mark.asyncio
async def test_configured_when_model_present(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some/model")
    assert await is_configured("t") is True


@pytest.mark.asyncio
async def test_not_configured_without_fallback_even_with_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some/model")
    monkeypatch.setenv("LLM_PLATFORM_FALLBACK", "false")
    assert await is_configured("t") is False


@pytest.mark.asyncio
async def test_complete_refuses_when_unconfigured(monkeypatch):
    """Must raise a typed error, not call out to a default provider.

    LiteLLM will happily try a provider inferred from the model string. With no
    model configured there is nothing to infer, and silently reaching some
    default endpoint would send a farmer's message somewhere nobody chose.
    """
    monkeypatch.setenv("LLM_MODEL", "")
    with pytest.raises(LLMUnconfigured):
        await complete([{"role": "user", "content": "hola"}])


@pytest.mark.asyncio
async def test_complete_unconfigured_never_calls_the_provider(monkeypatch):
    """Same property as above, checked structurally instead of by side effect.

    A test that only checks the exception type would still pass if the guard
    were accidentally moved after the provider call — as long as the (real,
    unmocked) call happened to also raise something. Failing the provider call
    hard on any invocation is what actually proves the early-exit exists.
    """

    async def fail_if_called(**kwargs):
        raise AssertionError("acompletion must not be called when unconfigured")

    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setattr("app.llm.provider.acompletion", fail_if_called)

    with pytest.raises(LLMUnconfigured):
        await complete([{"role": "user", "content": "hola"}])


@pytest.mark.asyncio
async def test_complete_passes_configured_values_through(monkeypatch):
    seen = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)

        class _Msg:
            content = "respuesta"
            tool_calls = None

        class _Choice:
            message = _Msg()

        class _Usage:
            prompt_tokens = 11
            completion_tokens = 7

        class _Resp:
            choices = [_Choice()]
            usage = _Usage()

        return _Resp()

    monkeypatch.setenv("LLM_MODEL", "some/model")
    monkeypatch.setenv("LLM_API_BASE", "http://example.invalid")
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)

    reply = await complete([{"role": "user", "content": "hola"}])

    assert seen["model"] == "some/model"
    assert seen["api_base"] == "http://example.invalid"
    assert reply.text == "respuesta"
    assert reply.tokens_prompt == 11
    assert reply.tokens_completion == 7
    assert reply.model == "some/model"


def _imports_litellm(source: str, filename: str) -> bool:
    """Whether a real `import litellm` / `from litellm import ...` node exists.

    Parses with `ast` and inspects the resulting Import/ImportFrom nodes
    rather than scanning text: a substring match would also fire on a
    comment, a docstring, or a string literal naming the library (this
    guard test's own source does all three), and would miss nothing a real
    import wouldn't already trip anyway — so it buys no precision, only
    false positives. A file that fails to parse is not a Python module this
    check can reason about either way, so it is skipped rather than crashing
    the whole guard.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "litellm" or alias.name.startswith("litellm.")
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module == "litellm" or node.module.startswith("litellm.")
            ):
                return True
    return False


@pytest.mark.asyncio
async def test_no_module_outside_the_facade_imports_litellm():
    """The facade is the only door to the provider.

    Asserted structurally rather than by convention: this is the property that
    makes swapping providers a one-file change, and conventions erode. Covers
    the whole backend/ tree — not just app/ — so a test file reaching around
    the facade is caught too, not only application code.
    """
    import pathlib

    backend_root = pathlib.Path(__file__).resolve().parents[1]
    facade = backend_root / "app" / "llm" / "provider.py"
    this_file = pathlib.Path(__file__).resolve()
    # .venv holds litellm's own installed source (thousands of real
    # `import litellm` statements) plus every other dependency; it is not
    # part of this repo (gitignored) and not what this property is about.
    excluded_dirs = {".venv", "__pycache__", ".pytest_cache"}

    offenders = []
    for p in backend_root.rglob("*.py"):
        rel = p.relative_to(backend_root)
        if any(part in excluded_dirs for part in rel.parts):
            continue
        if p in (facade, this_file):
            continue
        if _imports_litellm(p.read_text(), str(p)):
            offenders.append(rel.as_posix())

    assert offenders == [], f"litellm imported outside the facade: {offenders}"


# --- Secret-redaction: a provider failure must never surface the API key. ---
#
# Each test below configures a recognisable fake key, forces one failure path
# by making the mocked `acompletion` raise an exception whose message echoes
# the key back (worst case: a real provider partially or fully echoes a
# rejected credential in its error body), and then checks the key appears
# nowhere the caller — or anything reading logs — could see it:
#   1. the raised exception's own message,
#   2. the full traceback text (str(exc) alone would miss a leak sitting in a
#      chained __context__, which Python attaches to every exception raised
#      inside an `except` block unless silenced with `from None`),
#   3. captured log output.

FAKE_KEY = "sk-test-CANARY-2f9a7c1e4b"


def _configure_with_key(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some/model")
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)


def _assert_key_never_leaked(exc, caplog):
    import traceback

    full_traceback = "".join(traceback.format_exception(exc))
    assert FAKE_KEY not in str(exc)
    assert FAKE_KEY not in full_traceback
    assert FAKE_KEY not in caplog.text


@pytest.mark.asyncio
async def test_timeout_does_not_leak_key(monkeypatch, caplog):
    async def fake_acompletion(**kwargs):
        raise TimeoutError(f"upstream timed out (key={kwargs['api_key']})")

    _configure_with_key(monkeypatch)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(LLMProviderError) as exc_info:
            await complete([{"role": "user", "content": "hola"}])

    _assert_key_never_leaked(exc_info.value, caplog)


@pytest.mark.asyncio
async def test_auth_rejection_does_not_leak_key(monkeypatch, caplog):
    class VendorAuthError(Exception):
        """Stand-in for a vendor/library-specific exception (e.g. litellm's
        AuthenticationError), which must not reach the caller as-is either."""

    async def fake_acompletion(**kwargs):
        raise VendorAuthError(
            f"401 invalid credentials, received key {kwargs['api_key']}"
        )

    _configure_with_key(monkeypatch)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(LLMProviderError) as exc_info:
            await complete([{"role": "user", "content": "hola"}])

    _assert_key_never_leaked(exc_info.value, caplog)
    # And the vendor's own exception type must not be what the caller catches.
    assert not isinstance(exc_info.value, VendorAuthError)


@pytest.mark.asyncio
async def test_malformed_response_does_not_leak_key(monkeypatch, caplog):
    class _EmptyResp:
        choices = []  # indexing choices[0] raises IndexError

    async def fake_acompletion(**kwargs):
        assert kwargs["api_key"] == FAKE_KEY  # key was sent to the provider...
        return _EmptyResp()

    _configure_with_key(monkeypatch)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(LLMProviderError) as exc_info:
            await complete([{"role": "user", "content": "hola"}])

    # ...but must not come back out in the error raised about it.
    _assert_key_never_leaked(exc_info.value, caplog)


def _fake_resolved(**over):
    from app.llm.resolver import ResolvedProvider

    base = {
        "model": "tenant/model",
        "api_base": "",
        "api_key": "tenant-key",
        "origin": "tenant",
    }
    base.update(over)
    return ResolvedProvider(**base)


@pytest.mark.asyncio
async def test_complete_consumes_the_resolver(monkeypatch):
    seen = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)

        class _Msg:
            content = "ok"
            tool_calls = None

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = (_Choice(),)
            usage = None

        return _Resp()

    async def _resolved(tenant_id):
        return _fake_resolved()

    monkeypatch.setattr("app.llm.provider.resolve_provider", _resolved)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)

    reply = await complete([{"role": "user", "content": "hola"}], tenant_id="t")

    assert seen["model"] == "tenant/model"
    assert seen["api_key"] == "tenant-key"
    assert reply.model == "tenant/model"


@pytest.mark.asyncio
async def test_complete_resolver_none_never_calls_the_provider(monkeypatch):
    async def fail_if_called(**kwargs):
        raise AssertionError("acompletion must not be called when origin is none")

    async def _resolved(tenant_id):
        return _fake_resolved(model="", api_key="", origin="none", notice="blocked")

    monkeypatch.setattr("app.llm.provider.resolve_provider", _resolved)
    monkeypatch.setattr("app.llm.provider.acompletion", fail_if_called)

    with pytest.raises(LLMUnconfigured):
        await complete([{"role": "user", "content": "hola"}])


# --- BYOK: the tenant's own provider through the real resolver. ---

TENANT_KEY = "sk-tenant-CANARY-77be"


def _tenant_setup(monkeypatch, *, allowlist="provider-a"):
    from datetime import datetime, timezone

    from app.llm import resolver
    from app.llm.configs import TenantLLMConfig

    row = TenantLLMConfig(
        tenant_id="tenant-a", provider="provider-a", model="m1", api_base=None,
        api_key_ciphertext=TENANT_KEY.encode(), api_key_last4="77be",
        max_tokens_per_turn=None, updated_by="u", updated_at=datetime.now(timezone.utc),
    )

    async def fake_get(tenant_id):
        return row

    monkeypatch.setattr(resolver.configs, "get", fake_get)
    monkeypatch.setattr(resolver.keys, "decrypt", lambda ct: ct.decode())
    monkeypatch.setenv("LLM_ENDPOINT_ALLOWLIST", allowlist)
    monkeypatch.setenv("LLM_MODEL", "platform/model")
    monkeypatch.setenv("LLM_API_KEY", "platform-key")
    monkeypatch.setenv("LLM_PLATFORM_FALLBACK", "true")


class _OkResp:
    class _Msg:
        content = "ok"
        tool_calls = None

    class _Choice:
        message = None

    usage = None

    def __init__(self):
        self._Choice.message = self._Msg()
        self.choices = [self._Choice()]


@pytest.mark.asyncio
async def test_tenant_call_passes_provider_and_key(monkeypatch):
    seen = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)
        return _OkResp()

    _tenant_setup(monkeypatch)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)
    reply = await complete([{"role": "user", "content": "hola"}], tenant_id="tenant-a")
    assert seen["model"] == "provider-a/m1"
    assert seen["custom_llm_provider"] == "provider-a"
    assert seen["api_key"] == TENANT_KEY
    assert "api_base" not in seen
    assert reply.origin == "tenant"


@pytest.mark.asyncio
async def test_platform_call_has_no_custom_provider(monkeypatch):
    seen = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)
        return _OkResp()

    monkeypatch.setenv("LLM_MODEL", "some/model")
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)
    reply = await complete([{"role": "user", "content": "hola"}])
    assert "custom_llm_provider" not in seen
    assert reply.origin == "platform"


@pytest.mark.asyncio
async def test_blocked_tenant_never_calls_provider_even_with_platform(monkeypatch):
    async def fail_if_called(**kwargs):
        raise AssertionError("acompletion must not be called for a blocked tenant")

    _tenant_setup(monkeypatch, allowlist="provider-c")
    monkeypatch.setattr("app.llm.provider.acompletion", fail_if_called)
    with pytest.raises(LLMUnconfigured) as exc_info:
        await complete([{"role": "user", "content": "hola"}], tenant_id="tenant-a")
    assert exc_info.value.notice == "blocked"


@pytest.mark.asyncio
async def test_stored_url_model_never_reaches_the_provider(monkeypatch):
    from app.llm import resolver

    async def fail_if_called(**kwargs):
        raise AssertionError("acompletion must not be called for a URL model")

    _tenant_setup(monkeypatch)
    real_get = resolver.configs.get

    async def url_model(tenant_id):
        import dataclasses
        return dataclasses.replace(await real_get(tenant_id), model="http://orion.internal/x")

    monkeypatch.setattr(resolver.configs, "get", url_model)
    monkeypatch.setattr("app.llm.provider.acompletion", fail_if_called)
    with pytest.raises(LLMUnconfigured) as exc_info:
        await complete([{"role": "user", "content": "hola"}], tenant_id="tenant-a")
    assert exc_info.value.notice == "blocked"


@pytest.mark.asyncio
async def test_tenant_key_in_provider_error_is_redacted(monkeypatch, caplog):
    import traceback

    async def fake_acompletion(**kwargs):
        raise RuntimeError(f"401 bad key {kwargs['api_key']}")

    _tenant_setup(monkeypatch)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(LLMProviderError) as exc_info:
            await complete([{"role": "user", "content": "hola"}], tenant_id="tenant-a")
    assert "[redacted]" in str(exc_info.value)
    assert TENANT_KEY not in str(exc_info.value)
    assert TENANT_KEY not in "".join(traceback.format_exception(exc_info.value))
    assert TENANT_KEY not in caplog.text


def test_estimate_cost_is_none_for_an_unknown_model():
    from app.llm.provider import estimate_cost

    assert estimate_cost("nonexistent/model-xyz", 10, 5) is None


# --- failure kind + detail (panel "Test" button) ---------------------------

SYNTH_KEY = "sk-synthetic-ABCDEFGHIJ-0123456789/+="


def _lit_exc(name):
    import litellm

    import httpx

    cls = getattr(litellm, name)
    extra = {}
    if name == "PermissionDeniedError":
        extra["response"] = httpx.Response(
            403, request=httpx.Request("POST", "https://llm.example.test")
        )
    return cls(message="boom", model="m", llm_provider="provider-a", **extra)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("AuthenticationError", "auth_failed"),
        ("PermissionDeniedError", "auth_failed"),
        ("NotFoundError", "not_found"),
        ("RateLimitError", "quota_or_rate_limit"),
        ("BadRequestError", "bad_request"),
        ("Timeout", "unreachable"),
        ("APIConnectionError", "unreachable"),
        ("InternalServerError", "provider_error"),
    ],
)
@pytest.mark.asyncio
async def test_litellm_exception_maps_to_kind(monkeypatch, name, kind):
    async def fake_acompletion(**kwargs):
        raise _lit_exc(name)

    _configure_with_key(monkeypatch)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)
    with pytest.raises(LLMProviderError) as exc_info:
        await complete([{"role": "user", "content": "hola"}])
    assert exc_info.value.kind == kind


@pytest.mark.asyncio
async def test_non_litellm_exception_is_provider_error(monkeypatch):
    async def fake_acompletion(**kwargs):
        raise RuntimeError("x")

    _configure_with_key(monkeypatch)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)
    with pytest.raises(LLMProviderError) as exc_info:
        await complete([{"role": "user", "content": "hola"}])
    assert exc_info.value.kind == "provider_error"


@pytest.mark.asyncio
async def test_detail_never_contains_the_key_in_any_form(monkeypatch):
    import urllib.parse

    key = SYNTH_KEY
    quoted = urllib.parse.quote(key, safe="")
    truncated = key[:-4]

    async def fake_acompletion(**kwargs):
        raise RuntimeError(f"bad {key} / {quoted} / ...{truncated}... end")

    monkeypatch.setenv("LLM_MODEL", "some/model")
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)
    with pytest.raises(LLMProviderError) as exc_info:
        await complete([{"role": "user", "content": "hola"}])
    detail = exc_info.value.detail
    assert detail and "[redacted]" in detail
    for form in (key, quoted, truncated):
        assert form not in detail
        assert form not in str(exc_info.value)


def test_sanitize_strips_ansi_help_lines_and_collapses_whitespace():
    from app.llm.provider import _sanitize

    raw = (
        "\x1b[92mlitellm.AuthenticationError:\x1b[0m   bad\n\n  key  here\n"
        "Give Feedback / Get Help: https://example.test/issues\n"
        "LiteLLM.Info: If you need to debug this error, use it."
    )
    out = _sanitize(raw, "")
    assert "\x1b" not in out and "Give Feedback" not in out and "LiteLLM.Info" not in out
    assert out == "litellm.AuthenticationError: bad key here"


def test_sanitize_short_key_not_truncated_form():
    from app.llm.provider import _sanitize

    # keys shorter than 8 chars: only exact/quoted forms, no prefix redaction
    assert _sanitize("abc abcde", "abcdef") == "abc abcde"


@pytest.mark.asyncio
async def test_detail_is_cut_to_300(monkeypatch):
    async def fake_acompletion(**kwargs):
        raise RuntimeError("x" * 1000)

    _configure_with_key(monkeypatch)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)
    with pytest.raises(LLMProviderError) as exc_info:
        await complete([{"role": "user", "content": "hola"}])
    assert len(exc_info.value.detail) <= 300


@pytest.mark.asyncio
async def test_tools_call_reaches_the_network_layer_unmocked(monkeypatch):
    """NOT mocked: litellm imports `mcp` when tools are passed, and `mcp` is
    not a declared litellm dependency. A missing install surfaces as an
    APIConnectionError mentioning the module instead of a real connection
    failure. Port 9 (discard) on loopback refuses the connection."""
    import litellm

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "noop",
                "description": "x",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with pytest.raises(Exception) as exc_info:
        await litellm.acompletion(
            model="openai/x",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            api_base="http://127.0.0.1:9/v1",
            api_key="sk-synthetic-regression-0000",
            timeout=3,
            num_retries=0,
        )
    text = str(exc_info.value)
    assert "No module named" not in text
    # A real refused connection (litellm may classify it as a connection,
    # timeout or server error depending on version).
    assert isinstance(
        exc_info.value,
        litellm.APIConnectionError | litellm.Timeout | litellm.InternalServerError,
    ) and "onnection" in text


# --- more key forms --------------------------------------------------------

LONG_KEY = "sk-synthetic-Qw3rTy9uIoPaSdFgHj/KlZx+Cv=1234"


def _forms(key):
    import base64
    import urllib.parse

    b = key.encode()
    return {
        "plain": key,
        "quoted": urllib.parse.quote(key, safe=""),
        "split_newline": key[:20] + "\n" + key[20:],
        "split_spaces": key[:20] + "  \r\n  " + key[20:],
        "b64": base64.b64encode(b).decode(),
        "b64_nopad": base64.b64encode(b).decode().rstrip("="),
        "b64url": base64.urlsafe_b64encode(b).decode(),
        "b64url_nopad": base64.urlsafe_b64encode(b).decode().rstrip("="),
        "prefix_cut": key[:-4],
        "head_cut": key[4:],
        "tail_cut": key[:-9],
        "middle": key[6:-6],
    }


def _survivors(key, detail, n=8):
    squashed = "".join(detail.split())
    return [key[i : i + n] for i in range(len(key) - n + 1) if key[i : i + n] in squashed]


@pytest.mark.parametrize("form", list(_forms(LONG_KEY)))
def test_sanitize_redacts_every_key_form(form):
    from app.llm.provider import _sanitize

    echoed = _forms(LONG_KEY)[form]
    out = _sanitize(f"auth failed for {echoed} (retry)", LONG_KEY)
    assert "[redacted]" in out
    assert _survivors(LONG_KEY, out) == []
    assert out.startswith("auth failed for ") and out.endswith("(retry)")


def test_sanitize_keeps_unrelated_text():
    from app.llm.provider import _sanitize

    assert _sanitize("model not found: gpt-nope", LONG_KEY) == "model not found: gpt-nope"


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["split_newline", "b64", "head_cut"])
async def test_complete_detail_has_no_key_fragment(monkeypatch, form):
    echoed = _forms(LONG_KEY)[form]

    async def fake_acompletion(**kwargs):
        raise RuntimeError(f"rejected {echoed}")

    monkeypatch.setenv("LLM_MODEL", "some/model")
    monkeypatch.setenv("LLM_API_KEY", LONG_KEY)
    monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)
    with pytest.raises(LLMProviderError) as exc_info:
        await complete([{"role": "user", "content": "hola"}])
    assert _survivors(LONG_KEY, exc_info.value.detail) == []
    assert _survivors(LONG_KEY, str(exc_info.value)) == []


@pytest.mark.asyncio
async def test_internal_server_error_kind_depends_on_cause(monkeypatch):
    import httpx
    import litellm

    def make(msg, cause=None):
        e = litellm.InternalServerError(message=msg, model="m", llm_provider="provider-a")
        e.__cause__ = cause
        return e

    cases = [
        (make("boom"), "provider_error"),
        (make("upstream exploded"), "provider_error"),
        (make("OpenAIException - Connection error."), "unreachable"),
        (make("x", httpx.ConnectError("refused")), "unreachable"),
    ]
    _configure_with_key(monkeypatch)
    for exc, kind in cases:
        async def fake_acompletion(**kwargs):
            raise exc

        monkeypatch.setattr("app.llm.provider.acompletion", fake_acompletion)
        with pytest.raises(LLMProviderError) as ei:
            await complete([{"role": "user", "content": "hola"}])
        assert ei.value.kind == kind, str(exc)
