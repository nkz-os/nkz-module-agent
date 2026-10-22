"""Frozen tool catalog contract and its exposure through the registry."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from app.agent import registry
from app.tools.catalog import Citation, ToolResult, available_tools


def test_available_tools_exposes_core_and_module_tools():
    tools = available_tools()
    names = {t.name for t in tools}
    assert names == {
        "get_parcels",
        "get_parcel_snapshot",
        "get_timeseries",
        "get_active_alerts",
        "get_weather_conditions",
        "get_crop_trial_evidence",
        "check_substance_authorization",
    }
    # Core tools are not gated behind any installed module; the module-backed
    # adapters each carry the module id the registry filters on.
    gated = {t.name: t.required_module for t in tools if t.required_module}
    assert gated == {
        "get_active_alerts": "risk",
        "get_weather_conditions": "weather-map",
        "get_crop_trial_evidence": "bioorchestrator",
        "check_substance_authorization": "cue",
    }
    assert all(t.required_module is None for t in tools if t.name in {
        "get_parcels", "get_parcel_snapshot", "get_timeseries"})


@pytest.mark.usefixtures("configured_service_urls")
@pytest.mark.asyncio
async def test_registry_exposes_core_tool_specs():
    specs = await registry.tool_specs("tenant_test")
    assert len(specs) == 3
    assert all(s["type"] == "function" for s in specs)
    assert {s["function"]["name"] for s in specs} == {
        "get_parcels",
        "get_parcel_snapshot",
        "get_timeseries",
    }
    assert await registry.has_tools("tenant_test") is True


def test_citation_is_frozen():
    c = Citation(
        source="orion:AgriParcel", retrieved_at=datetime.now(timezone.utc), detail="x"
    )
    with pytest.raises(FrozenInstanceError):
        c.source = "other"  # type: ignore[misc]


def test_tool_result_defaults_unavailable_reason_to_none():
    r = ToolResult(ok=True, payload={}, citations=())
    assert r.unavailable_reason is None


def test_every_required_setting_is_a_real_settings_field():
    """A misspelt required_setting would hide its tool forever, silently."""
    from app.config import Settings

    for tool in available_tools():
        if tool.required_setting is not None:
            assert tool.required_setting in Settings.model_fields, tool.name
