import dataclasses
import logging

import pytest

from app.config import get_settings
from app.llm import resolver
from app.llm.resolver import ResolvedProvider, resolve_provider, tenant_provider


@pytest.fixture(autouse=True)
def clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_fallback_by_default(monkeypatch):
    monkeypatch.delenv("LLM_PLATFORM_FALLBACK", raising=False)


def _configure_platform(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some/model")
    monkeypatch.setenv("LLM_API_BASE", "http://example.invalid")
    monkeypatch.setenv("LLM_API_KEY", "platform-key")


def _clear_platform(monkeypatch):
    for name in ("LLM_MODEL", "LLM_API_BASE", "LLM_API_KEY"):
        monkeypatch.setenv(name, "")


pytestmark = pytest.mark.asyncio

KEY = "sk-test-CANARY-91ac"


def _row(provider="anthropic", model="m1", api_base=None, key=KEY):
    from app.llm.configs import TenantLLMConfig
    from datetime import datetime, timezone

    return TenantLLMConfig(
        tenant_id="tenant-a", provider=provider, model=model, api_base=api_base,
        api_key_ciphertext=key.encode(), api_key_last4=key[-4:],
        max_tokens_per_turn=None, updated_by="u", updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def tenant_row(monkeypatch):
    """Control the tenant row; the key 'decrypts' to the ciphertext text."""
    state = {"row": None}

    async def fake_get(tenant_id):
        return state["row"]

    monkeypatch.setattr(resolver.configs, "get", fake_get)
    monkeypatch.setattr(resolver.keys, "decrypt", lambda ct: ct.decode())
    monkeypatch.setenv("LLM_ENDPOINT_ALLOWLIST", "anthropic")
    return state


async def test_no_row_no_fallback_is_none_even_with_platform_set(monkeypatch, tenant_row):
    _configure_platform(monkeypatch)
    got = await resolve_provider("tenant-a")
    assert got.origin == "none"
    assert got.notice is None
    assert (got.model, got.api_key) == ("", "")
    assert await tenant_provider("tenant-a") is None


async def test_no_row_with_fallback_is_platform(monkeypatch, tenant_row):
    _configure_platform(monkeypatch)
    monkeypatch.setenv("LLM_PLATFORM_FALLBACK", "true")
    got = await resolve_provider("tenant-a")
    assert got == ResolvedProvider(
        model="some/model", api_base="http://example.invalid",
        api_key="platform-key", origin="platform",
    )


async def test_fallback_without_platform_model_is_none(monkeypatch, tenant_row):
    _clear_platform(monkeypatch)
    monkeypatch.setenv("LLM_PLATFORM_FALLBACK", "true")
    assert (await resolve_provider("tenant-a")).origin == "none"


async def test_no_tenant_id_never_reads_a_row(monkeypatch, tenant_row):
    _configure_platform(monkeypatch)
    monkeypatch.setenv("LLM_PLATFORM_FALLBACK", "true")
    tenant_row["row"] = _row()
    assert (await resolve_provider(None)).origin == "platform"


async def test_allowed_row_resolves_to_tenant(monkeypatch, tenant_row):
    _configure_platform(monkeypatch)
    tenant_row["row"] = _row()
    got = await resolve_provider("tenant-a")
    assert got.origin == "tenant"
    assert got.model == "anthropic/m1"
    assert got.provider == "anthropic"
    assert got.api_key == KEY
    assert got.api_base == ""
    assert got.notice is None


async def test_tenant_api_base_passes_through_when_allowlisted(monkeypatch, tenant_row):
    monkeypatch.setenv("LLM_ENDPOINT_ALLOWLIST", "openai@https://llm.example.test")
    tenant_row["row"] = _row(provider="openai", api_base="https://llm.example.test/v1")
    got = await resolve_provider("tenant-a")
    assert got.origin == "tenant"
    assert got.api_base == "https://llm.example.test/v1"


async def test_narrowed_allowlist_blocks_and_never_uses_platform(monkeypatch, tenant_row):
    _configure_platform(monkeypatch)
    monkeypatch.setenv("LLM_PLATFORM_FALLBACK", "true")
    monkeypatch.setenv("LLM_ENDPOINT_ALLOWLIST", "deepseek")
    tenant_row["row"] = _row()
    got = await resolve_provider("tenant-a")
    assert got.origin == "none"
    assert got.notice == "blocked"
    assert got.api_key == ""


async def test_empty_allowlist_blocks(monkeypatch, tenant_row):
    monkeypatch.setenv("LLM_ENDPOINT_ALLOWLIST", "")
    tenant_row["row"] = _row()
    assert (await resolve_provider("tenant-a")).notice == "blocked"


async def test_unreadable_key_logs_error_without_key(monkeypatch, tenant_row, caplog):
    from app.llm.keys import KeyUnreadable

    def boom(ct):
        raise KeyUnreadable("cannot decrypt")

    monkeypatch.setattr(resolver.keys, "decrypt", boom)
    _configure_platform(monkeypatch)
    monkeypatch.setenv("LLM_PLATFORM_FALLBACK", "true")
    tenant_row["row"] = _row()
    with caplog.at_level(logging.ERROR):
        got = await resolve_provider("tenant-a")
    assert got.origin == "none"
    assert got.notice == "key_unreadable"
    assert "byok_key_unreadable tenant_id=tenant-a" in caplog.text
    assert KEY not in caplog.text


async def test_resolved_provider_is_frozen():
    got = ResolvedProvider(model="m", api_base="", api_key="", origin="none")
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.model = "other"
