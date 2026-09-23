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


def test_unconfigured_when_no_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "")
    assert is_configured() is False


def test_configured_when_model_present(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some/model")
    assert is_configured() is True


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
