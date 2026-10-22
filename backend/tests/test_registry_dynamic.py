"""Dynamic tool registry: catalogue ∩ tenant's enabled module installs.

These tests use a synthetic module-gated ToolDefinition alongside the real
core adapters, so they never depend on the Task 3 tools (which do not exist
yet). The DB is the test stand-in for ``public.module_installations`` created
by the db_pool fixture.
"""

import logging
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from app.agent import registry
from app.tools.catalog import ToolDefinition, ToolResult, available_tools
from tests.conftest import requires_db

pytestmark = [
    requires_db,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("configured_service_urls"),
]


class _Args(BaseModel):
    x: int = 0


async def _handler(ctx, args):
    return ToolResult(ok=True, payload={"x": args.x}, citations=())


FAKE_MODULE_TOOL = ToolDefinition(
    name="fake_module_tool",
    description="synthetic module-gated tool",
    args_model=_Args,
    handler=_handler,
    required_module="fake_module",
)

CORE_NAMES = {"get_parcels", "get_parcel_snapshot", "get_timeseries"}


def _patch_catalog(monkeypatch):
    """Catalogue = the three core adapters plus one module-gated synthetic."""
    monkeypatch.setattr(
        registry, "available_tools", lambda: available_tools() + (FAKE_MODULE_TOOL,)
    )


async def _insert(pool, tenant_id, module_id, enabled):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO public.module_installations "
            "(module_id, tenant_id, is_enabled, installed_at) "
            "VALUES ($1, $2, $3, $4)",
            module_id,
            tenant_id,
            enabled,
            datetime.now(timezone.utc),
        )


async def test_core_tools_are_always_available(db_pool, monkeypatch):
    _patch_catalog(monkeypatch)
    registry.clear_cache()

    tools = await registry.tools_for("tenant_with_no_rows")

    assert {t.name for t in tools} == CORE_NAMES


async def test_module_tool_filtered_by_enabled_installations(db_pool, monkeypatch):
    """Mutation (b): returning the whole catalogue would surface a module tool
    for a tenant that has not installed it -- this must go red on that."""
    _patch_catalog(monkeypatch)
    registry.clear_cache()

    names = {t.name for t in await registry.tools_for("tenant_none")}
    assert names == CORE_NAMES
    assert "fake_module_tool" not in names

    await _insert(db_pool, "tenant_on", "fake_module", True)
    names = {t.name for t in await registry.tools_for("tenant_on")}
    assert "fake_module_tool" in names

    await _insert(db_pool, "tenant_off", "fake_module", False)
    names = {t.name for t in await registry.tools_for("tenant_off")}
    assert "fake_module_tool" not in names


async def test_fail_open_returns_core_subset_and_warns(db_pool, monkeypatch, caplog):
    """Mutation (a): a broken installs query must not explode the turn. Remove
    the try/except around the query and this test goes red with an exception."""
    _patch_catalog(monkeypatch)
    registry.clear_cache()
    await _insert(db_pool, "tenant_a", "fake_module", True)

    async with db_pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE public.module_installations "
            "RENAME TO module_installations_broken"
        )
    try:
        with caplog.at_level(logging.WARNING):
            tools = await registry.tools_for("tenant_a")
        assert {t.name for t in tools} == CORE_NAMES
        assert "registry_installations_query_failed" in caplog.text
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE public.module_installations_broken "
                "RENAME TO module_installations"
            )
        registry.clear_cache()


async def test_cache_is_keyed_by_tenant_id(db_pool, monkeypatch):
    """Mutation (c): a cache keyed by anything but tenant_id lets tenant B see
    tenant A's cached tool list. B must re-read its own (empty) installs."""
    _patch_catalog(monkeypatch)
    registry.clear_cache()
    await _insert(db_pool, "tenant_a", "fake_module", True)

    names_a = {t.name for t in await registry.tools_for("tenant_a")}
    assert "fake_module_tool" in names_a

    names_b = {t.name for t in await registry.tools_for("tenant_b")}
    assert "fake_module_tool" not in names_b


async def test_cache_expires_after_ttl(db_pool, monkeypatch):
    _patch_catalog(monkeypatch)
    registry.clear_cache()
    await _insert(db_pool, "tenant_a", "fake_module", True)

    class _Clock:
        def __init__(self):
            self.t = 0.0

        def monotonic(self):
            return self.t

    clock = _Clock()
    monkeypatch.setattr(registry, "time", clock)

    names = {t.name for t in await registry.tools_for("tenant_a")}
    assert "fake_module_tool" in names

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM public.module_installations WHERE tenant_id = 'tenant_a'"
        )

    # Still inside the TTL: the stale cached list is served, not re-read.
    names = {t.name for t in await registry.tools_for("tenant_a")}
    assert "fake_module_tool" in names

    # Past the TTL: the row is gone, so the refreshed list drops the tool.
    clock.t = 61.0
    names = {t.name for t in await registry.tools_for("tenant_a")}
    assert "fake_module_tool" not in names


# --- An empty service URL disables its tool. ---


@pytest.mark.parametrize(
    "env,tool,module",
    [
        ("TIMESERIES_URL", "get_timeseries", None),
        ("WEATHER_MAP_URL", "get_weather_conditions", "weather-map"),
        ("BIOORCH_API_URL", "get_crop_trial_evidence", "bioorchestrator"),
        ("CUE_API_URL", "check_substance_authorization", "cue"),
    ],
)
async def test_unconfigured_url_hides_the_tool(db_pool, monkeypatch, env, tool, module):
    from app.config import get_settings

    if module:
        await _insert(db_pool, "tenant_url", module, True)

    registry.clear_cache()
    assert tool in {t.name for t in await registry.tools_for("tenant_url")}

    monkeypatch.setenv(env, "")
    get_settings.cache_clear()
    registry.clear_cache()
    assert tool not in {t.name for t in await registry.tools_for("tenant_url")}


async def test_fail_open_subset_is_core_intersect_configured(db_pool, monkeypatch):
    from app.config import get_settings

    async def boom(tenant_id):
        raise RuntimeError("view gone")

    monkeypatch.setattr(registry, "_installed_module_ids", boom)
    monkeypatch.setenv("TIMESERIES_URL", "")
    get_settings.cache_clear()
    registry.clear_cache()
    names = {t.name for t in await registry.tools_for("tenant_x")}
    assert names == {"get_parcels", "get_parcel_snapshot"}
