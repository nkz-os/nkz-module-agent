"""Timeseries-reader adapter.

Goes through the core HTTP reader (never direct SQL -- see the platform rule
that telemetry must flow through Orion-LD subscriptions, and the reader service
is the only holder of the timeseries DB credentials). Auth is the canonical
module -> core HMAC: X-Tenant-ID + X-Auth-Signature from
generate_hmac_signature(HMAC_SECRET, "", tenant).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from enum import Enum

import httpx
from nkz_platform_sdk.crypto import generate_hmac_signature
from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings
from app.domain.session import SessionContext
from app.tools.catalog import Citation, ParcelId, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8.0
_LIMIT = 1000
# Points handed to the model: the newest ones; the summary covers them all.
_MAX_POINTS = 50


class Metric(str, Enum):
    soil_moisture = "soil_moisture"
    soil_temperature = "soil_temperature"
    air_temperature = "air_temperature"
    air_humidity = "air_humidity"
    precipitation = "precipitation"
    ndvi = "ndvi"


# Frozen metric -> entity_type map. DeviceMeasurement for the sensor metrics,
# EOProduct for the satellite NDVI product.
METRIC_ENTITY_TYPES: dict[Metric, str] = {
    Metric.soil_moisture: "DeviceMeasurement",
    Metric.soil_temperature: "DeviceMeasurement",
    Metric.air_temperature: "DeviceMeasurement",
    Metric.air_humidity: "DeviceMeasurement",
    Metric.precipitation: "DeviceMeasurement",
    Metric.ndvi: "EOProduct",
}


class SeriesPoint(BaseModel):
    observed_at: str
    value: float | str


class SeriesExtreme(BaseModel):
    value: float
    observed_at: str


class SeriesSummary(BaseModel):
    """min/max/mean/last over every numeric point the reader returned."""

    count: int
    min: SeriesExtreme
    max: SeriesExtreme
    mean: float
    last: SeriesExtreme


class TimeseriesResult(BaseModel):
    metric: str
    total_points: int
    points: list[SeriesPoint]  # the newest _MAX_POINTS, newest first
    summary: SeriesSummary | None = None


class GetTimeseriesArgs(BaseModel):
    parcel_id: ParcelId
    metric: Metric
    days: int = Field(default=7, ge=1, le=90)


def _point_value(raw: object) -> float | str | None:
    """A reader measurement as a number or short text; anything else is dropped."""
    if isinstance(raw, dict):
        raw = raw.get("value")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and len(raw) <= 64:
        return raw
    return None


def _shape(metric: str, body: object) -> TimeseriesResult | None:
    """Freeze the reader body into at most _MAX_POINTS newest points + a summary.

    The reader answers newest-first on this route, but order is never trusted:
    points are sorted by timestamp here, so an ascending reader cannot make
    the newest data the part that is cut. None means a malformed body.
    """
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    points: list[SeriesPoint] = []
    for row in body["data"]:
        if not isinstance(row, dict) or not isinstance(row.get("observed_at"), str):
            continue
        value = _point_value(row.get(metric))
        if value is None:
            continue
        points.append(SeriesPoint(observed_at=row["observed_at"], value=value))
    points.sort(key=lambda p: p.observed_at, reverse=True)

    numeric = [p for p in points if isinstance(p.value, float)]
    summary = None
    if numeric:
        low = min(numeric, key=lambda p: p.value)
        high = max(numeric, key=lambda p: p.value)
        last = numeric[0]
        summary = SeriesSummary(
            count=len(numeric),
            min=SeriesExtreme(value=low.value, observed_at=low.observed_at),
            max=SeriesExtreme(value=high.value, observed_at=high.observed_at),
            mean=sum(p.value for p in numeric) / len(numeric),
            last=SeriesExtreme(value=last.value, observed_at=last.observed_at),
        )
    try:
        return TimeseriesResult(
            metric=metric,
            total_points=len(points),
            points=points[:_MAX_POINTS],
            summary=summary,
        )
    except ValidationError:
        return None


async def get_timeseries(ctx: SessionContext, args: GetTimeseriesArgs) -> ToolResult:
    settings = get_settings()
    if not settings.timeseries_url:
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="timeseries unreachable",
        )

    entity_type = METRIC_ENTITY_TYPES[args.metric]
    now = datetime.now(timezone.utc)
    params = {
        "from": (now - timedelta(days=args.days)).isoformat(),
        "to": now.isoformat(),
        "attrs": args.metric.value,
        "limit": _LIMIT,
    }
    headers = {
        "X-Tenant-ID": ctx.tenant_id,
        "X-Auth-Signature": generate_hmac_signature(
            settings.hmac_secret, "", ctx.tenant_id
        ),
    }
    url = (
        f"{settings.timeseries_url.rstrip('/')}"
        f"/api/timeseries/type/{entity_type}/parcel/{args.parcel_id}/data"
    )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        # Operator signal only: the model-facing reason stays generic so no
        # URL, header or signature detail can reach the conversation.
        logger.warning("tool_get_timeseries_error", exc_info=True)
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="timeseries unreachable",
        )

    result = _shape(args.metric.value, body)
    if result is None:
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="timeseries unreachable",
        )

    return ToolResult(
        ok=True,
        payload=result.model_dump(exclude_none=True),
        citations=(
            Citation(
                source="timeseries-reader",
                retrieved_at=now,
                detail=(
                    f"parcel={args.parcel_id} metric={args.metric.value} "
                    f"days={args.days}"
                ),
            ),
        ),
    )


GET_TIMESERIES = ToolDefinition(
    name="get_timeseries",
    description=(
        "Read a metric timeseries for one parcel from the platform reader. "
        "metric is one of soil_moisture, soil_temperature, air_temperature, "
        "air_humidity, precipitation, ndvi."
    ),
    args_model=GetTimeseriesArgs,
    handler=get_timeseries,
    required_module=None,
    required_setting="timeseries_url",
)
