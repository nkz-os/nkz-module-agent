"""Weather-map adapter (module-backed read tool).

Reads per-parcel zonal weather statistics from the weather-map module. Auth is
the module's real ``require_tenant`` contract: X-Tenant-ID AND X-User-ID (no
HMAC validation today). X-Auth-Signature is kept as a harmless,
forward-compatible header for when weather-map adopts the platform HMAC.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from nkz_platform_sdk.crypto import generate_hmac_signature
from pydantic import BaseModel, Field, ValidationError

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
    # Optional: present once weather-map labels its snapshot (kind, and the day
    # the values are valid for). Never required, never defaulted.
    kind: str | None = None
    valid_date: str | None = None
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
            kind=body.get("kind") if isinstance(body.get("kind"), str) else None,
            valid_date=(
                body.get("valid_date")
                if isinstance(body.get("valid_date"), str)
                else None
            ),
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


# --- weather-api: daily series with source and date ---------------------------

_DAILY_UNAVAILABLE = "daily weather unreachable"
_DAY_KEYS = (
    "date",
    "kind",
    "tmin_c",
    "tmax_c",
    "precip_mm",
    "et0_mm",
    "hours_below_zero",
    "source",
    "sources",
)
# Only the nights not yet closed carry the frost-warning value, under a key
# that cannot be mistaken for the expected minimum.
_FROST_KINDS = ("running", "forecast")
_FROST_NOTE = (
    "frost_warning_min_c is the colder of two models and is used for frost "
    "warnings; it is not the expected minimum (tmin_c is)."
)
_PASS_THROUGH = (
    "local_today",
    "forecast_issued_at",
    "missing_days",
    "missing_reasons",
    "totals",
)
# Before this local hour the running day has not reached its overnight minimum
# yet, so "last night" is the latest closed day instead.
_LAST_NIGHT_FROM_HOUR = 10


class GetParcelDailyWeatherArgs(BaseModel):
    parcel_id: ParcelId
    past_days: int = Field(default=7, ge=0, le=30)
    forecast_days: int = Field(default=7, ge=0, le=15)


def _local_hour(local_now: Any) -> int | None:
    if not isinstance(local_now, str):
        return None
    try:
        return datetime.fromisoformat(local_now).hour
    except ValueError:
        return None


def _last_night(days: list[dict], local_now: Any) -> tuple[dict | None, str | None]:
    """Last night's minimum and a note when it is not the running night.

    From 10:00 local the running day's tmin_c; before that, or when the local
    hour or the running day is unavailable, the latest closed day, labelled as
    that closed day. Returns (None, note) when nothing fits.
    """
    hour = _local_hour(local_now)
    if hour is not None and hour >= _LAST_NIGHT_FROM_HOUR:
        running = [d for d in days if d.get("kind") == "running"]
        if running:
            day = max(running, key=lambda d: d["date"])
            if day.get("tmin_c") is not None:
                return _night(day), None
    closed = [
        d for d in days if d.get("kind") == "closed" and d.get("tmin_c") is not None
    ]
    if not closed:
        return None, "no closed day with a minimum is available"
    day = max(closed, key=lambda d: d["date"])
    note = (
        f"closed day {day['date']}; the running night is not available"
        if hour is None or hour >= _LAST_NIGHT_FROM_HOUR
        else f"closed day {day['date']}; the running day has not reached its minimum yet"
    )
    return _night(day), note


def _night(day: dict) -> dict:
    return {
        k: day[k] for k in ("date", "kind", "tmin_c", "source", "sources") if k in day
    }


async def get_parcel_daily_weather(
    ctx: SessionContext, args: GetParcelDailyWeatherArgs
) -> ToolResult:
    settings = get_settings()
    if not settings.weather_api_url:
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason=_DAILY_UNAVAILABLE,
        )

    url = (
        f"{settings.weather_api_url.rstrip('/')}"
        f"/api/weather/parcel/{args.parcel_id}/daily"
    )
    headers = {
        "X-Tenant-ID": ctx.tenant_id,
        "X-User-ID": ctx.user_id,
        "X-Auth-Signature": generate_hmac_signature(
            settings.hmac_secret, "", ctx.tenant_id
        ),
    }
    # ASSUMPTION: the range is anchored on the UTC date; the service labels
    # each day in the parcel's own time zone and reports local_today.
    end = datetime.now(timezone.utc).date()
    params = {
        "start": (end - timedelta(days=args.past_days)).isoformat(),
        "end": end.isoformat(),
        "fill": "open_meteo",
        "forecast_days": max(1, args.forecast_days),
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_get_parcel_daily_weather_error", exc_info=True)
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason=_DAILY_UNAVAILABLE,
        )

    raw_days = body.get("days") if isinstance(body, dict) else None
    if not isinstance(raw_days, list) or not all(
        isinstance(d, dict) and isinstance(d.get("date"), str) for d in raw_days
    ):
        logger.warning("tool_get_parcel_daily_weather_malformed")
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason=_DAILY_UNAVAILABLE,
        )

    local_today = body.get("local_today")
    has_local_today = isinstance(local_today, str) and bool(local_today)
    keep_forecast = args.forecast_days > 0 and has_local_today
    days = []
    for d in raw_days:
        if d.get("kind") == "forecast" and not keep_forecast:
            continue
        out = {k: d[k] for k in _DAY_KEYS if k in d}
        if d.get("kind") in _FROST_KINDS and d.get("tmin_coldest_c") is not None:
            out["frost_warning_min_c"] = d["tmin_coldest_c"]
        days.append(out)
    payload: dict[str, Any] = {k: body[k] for k in _PASS_THROUGH if k in body}
    payload["days"] = days
    if any("frost_warning_min_c" in d for d in days):
        payload["field_notes"] = _FROST_NOTE
    if args.forecast_days > 0 and not has_local_today:
        # Without the parcel's local date a forecast day cannot be placed;
        # never guess it.
        payload["forecast_unavailable"] = (
            "the parcel's local date is unknown, so forecast days are withheld"
        )
    last_night, note = _last_night(days, body.get("local_now"))
    if last_night is not None:
        payload["last_night"] = last_night
    if note:
        payload["last_night_note"] = note

    return ToolResult(
        ok=True,
        payload=payload,
        citations=(
            Citation(
                source="weather-api:daily",
                retrieved_at=datetime.now(timezone.utc),
                detail=(
                    f"parcel={args.parcel_id} start={params['start']} "
                    f"end={params['end']} forecast_days={args.forecast_days}"
                ),
            ),
        ),
    )


GET_WEATHER_CONDITIONS = ToolDefinition(
    name="get_weather_conditions",
    description=(
        "Read a daily raster snapshot of zonal weather for one parcel from "
        "the weather-map module (temperature, frost risk, eto, water balance, "
        "solar radiation and soil moisture) with its date. It is a snapshot "
        "for spatial distribution inside the parcel, not measured data. Do "
        "not use it for yesterday's or last night's minimum or for "
        "forecasts: use get_parcel_daily_weather."
    ),
    args_model=GetWeatherConditionsArgs,
    handler=get_weather_conditions,
    required_module="weather-map",
    required_setting="weather_map_url",
)


GET_PARCEL_DAILY_WEATHER = ToolDefinition(
    name="get_parcel_daily_weather",
    description=(
        "Read the daily weather series of one parcel: past days, today and "
        "forecast days (past_days up to 30, forecast_days up to 15, 0 for none). Each day "
        "has date, kind (closed, running or forecast), tmin_c, tmax_c, "
        "precip_mm, et0_mm, hours_below_zero, source and per-variable "
        "sources. All values are model estimates unless the source is "
        "iot_sensor, which is measured; say so. Always state the source and "
        "the date of each figure. 'Last night' is the running day's tmin_c "
        "from 10:00 local time, otherwise the latest closed day: use the "
        "last_night field and its last_night_note. frost_warning_min_c "
        "(running and forecast days only) is the colder of two models, a "
        "frost-warning value; never present it as the expected minimum. If "
        "forecast_unavailable or missing_days is present, say the data is "
        "missing rather than estimating it."
    ),
    args_model=GetParcelDailyWeatherArgs,
    handler=get_parcel_daily_weather,
    required_module=None,
    required_setting="weather_api_url",
)
