"""Weather-map adapter (module-backed read tool).

Reads per-parcel zonal weather statistics from the weather-map module. Auth is
the module's real ``require_tenant`` contract: X-Tenant-ID AND X-User-ID (no
HMAC validation today). X-Auth-Signature is kept as a harmless,
forward-compatible header for when weather-map adopts the platform HMAC.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from nkz_platform_sdk.crypto import generate_hmac_signature
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.domain.session import SessionContext
from app.tools.catalog import Citation, ParcelId, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8.0

# Frozen metric surface of the weather-map stats endpoint. It comes from
# ``metrics`` in nkz-module-weather-map/backend/app/config.py; the endpoint
# rejects anything else with 400 (tiles.py: invalid metrics -> HTTPException).
FROZEN_METRICS = (
    "temperature_avg",
    "temperature_min",
    "solar_radiation",
    "eto",
    "water_balance",
    "frost_risk",
    "soil_moisture",
)


class MetricStats(BaseModel):
    """Per-metric zonal statistics; the upstream histogram and pixel count are
    dropped. Unknown keys are ignored, so the surface is exactly this one."""

    mean: float | None = None
    min: float | None = None
    max: float | None = None
    std: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    deficit_area_pct: float | None = None
    severe_deficit_pct: float | None = None
    high_risk_pct: float | None = None
    moderate_risk_pct: float | None = None
    dry_pct: float | None = None
    saturated_pct: float | None = None
    heat_stress_pct: float | None = None
    frost_pct: float | None = None
    error: str | None = None


class WeatherConditions(BaseModel):
    """Frozen /stats surface: the date and the requested metrics. The parcel
    polygon the endpoint also returns is not passed on."""

    date: str | None = None
    metrics: dict[str, MetricStats]


class GetWeatherConditionsArgs(BaseModel):
    parcel_id: ParcelId


async def get_weather_conditions(
    ctx: SessionContext, args: GetWeatherConditionsArgs
) -> ToolResult:
    settings = get_settings()
    if not settings.weather_map_url:
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="weather map unreachable",
        )

    url = (
        f"{settings.weather_map_url.rstrip('/')}"
        f"/api/weather-map/stats/{args.parcel_id}"
    )
    headers = {
        "X-Tenant-ID": ctx.tenant_id,
        "X-User-ID": ctx.user_id,
        "X-Auth-Signature": generate_hmac_signature(
            settings.hmac_secret, "", ctx.tenant_id
        ),
    }
    params = {"metrics": ",".join(FROZEN_METRICS)}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        # Operator signal only: the model-facing reason stays generic so no
        # URL, header or signature detail can reach the conversation.
        logger.warning("tool_get_weather_conditions_error", exc_info=True)
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="weather map unreachable",
        )

    if not isinstance(body, dict):
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="weather map unreachable",
        )

    try:
        raw_metrics = body.get("metrics")
        conditions = WeatherConditions(
            date=body.get("date") if isinstance(body.get("date"), str) else None,
            metrics={
                name: MetricStats.model_validate(stats)
                for name, stats in (
                    raw_metrics.items() if isinstance(raw_metrics, dict) else ()
                )
                if name in FROZEN_METRICS and isinstance(stats, dict)
            },
        )
    except ValidationError:
        logger.warning("tool_get_weather_conditions_malformed", exc_info=True)
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="weather map unreachable",
        )

    return ToolResult(
        ok=True,
        payload=conditions.model_dump(exclude_none=True),
        citations=(
            Citation(
                source="weather-map:stats",
                retrieved_at=datetime.now(timezone.utc),
                detail=f"parcel={args.parcel_id} date={conditions.date}",
            ),
        ),
    )


GET_WEATHER_CONDITIONS = ToolDefinition(
    name="get_weather_conditions",
    description=(
        "Read current zonal weather conditions for one parcel from the "
        "weather-map module (temperature, frost risk, eto, water balance, "
        "solar radiation and soil moisture) with their date."
    ),
    args_model=GetWeatherConditionsArgs,
    handler=get_weather_conditions,
    required_module="weather-map",
    required_setting="weather_map_url",
)
