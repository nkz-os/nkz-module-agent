import dataclasses

import pytest

from app.config import get_settings
from app.llm import resolver
from app.llm.resolver import ResolvedProvider, resolve_provider, tenant_provider


@pytest.fixture(autouse=True)
def clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure_platform(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some/model")
    monkeypatch.setenv("LLM_API_BASE", "http://example.invalid")
    monkeypatch.setenv("LLM_API_KEY", "platform-key")


def _clear_platform(monkeypatch):
    for name in ("LLM_MODEL", "LLM_API_BASE", "LLM_API_KEY"):
        monkeypatch.setenv(name, "")


def test_platform_settings_resolve_to_platform(monkeypatch):
    _configure_platform(monkeypatch)
    got = resolve_provider("tenant_a")
    assert got == ResolvedProvider(
        model="some/model",
        api_base="http://example.invalid",
        api_key="platform-key",
        origin="platform",
    )


def test_nothing_configured_resolves_to_none(monkeypatch):
    _clear_platform(monkeypatch)
    got = resolve_provider("tenant_a")
    assert got.origin == "none"
    assert (got.model, got.api_base, got.api_key) == ("", "", "")


def test_tenant_branch_is_a_pinned_stub(monkeypatch):
    """F3c fills this in; until then a tenant can never resolve to its own provider."""
    _configure_platform(monkeypatch)
    assert tenant_provider("tenant_a") is None
    assert resolve_provider("tenant_a").origin == "platform"


def test_tenant_provider_wins_over_platform(monkeypatch):
    _configure_platform(monkeypatch)
    tenant = ResolvedProvider(
        model="tenant/model", api_base="", api_key="tenant-key", origin="tenant"
    )
    monkeypatch.setattr(resolver, "tenant_provider", lambda tenant_id: tenant)

    assert resolve_provider("tenant_a") == tenant, (
        "platform must not shadow a tenant provider"
    )
    assert resolve_provider(None).origin == "platform"


def test_resolved_provider_is_frozen():
    got = ResolvedProvider(model="m", api_base="", api_key="", origin="none")
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.model = "other"
