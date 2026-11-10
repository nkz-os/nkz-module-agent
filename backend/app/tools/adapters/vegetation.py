"""Vegetation adapter: NDVI of one parcel over the last six months.

Auth is X-Tenant-ID + X-User-ID. The route takes the full parcel URN.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import get_settings
from app.domain.session import SessionContext
from app.tools.catalog import Citation, ParcelId, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 12.0
_UNAVAILABLE = "vegetation scenes unreachable"
_NO_SCENES = "No satellite scenes in the last six months."
_MAX_POINTS = 30


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Point(_Frozen):
    date: str
    mean: float | None = None
    min: float | None = None
    max: float | None = None


class Summary(_Frozen):
    avg: float | None = None
    min: float | None = None
    max: float | None = None
    count: int | None = None


class GetVegetationTrendArgs(BaseModel):
    parcel_id: ParcelId


def _fail() -> ToolResult:
    return ToolResult(
        ok=False, payload=None, citations=(), unavailable_reason=_UNAVAILABLE
    )


def _result(payload: dict, parcel_id: str) -> ToolResult:
    return ToolResult(
        ok=True,
        payload=payload,
        citations=(
            Citation(
                source="vegetation:scenes/stats",
                retrieved_at=datetime.now(timezone.utc),
                detail=f"parcel={parcel_id} index=NDVI",
            ),
        ),
    )


def _empty(parcel_id: str) -> ToolResult:
    return _result(
        {"index": "NDVI", "points": [], "summary": {}, "note": _NO_SCENES},
        parcel_id,
    )


async def get_vegetation_trend(
    ctx: SessionContext, args: GetVegetationTrendArgs
) -> ToolResult:
    settings = get_settings()
    if not settings.vegetation_url:
        return _fail()

    url = (
        f"{settings.vegetation_url.rstrip('/')}/api/vegetation/scenes/"
        f"{quote(args.parcel_id, safe='')}/stats"
    )
    headers = {"X-Tenant-ID": ctx.tenant_id, "X-User-ID": ctx.user_id}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url, params={"index_type": "NDVI", "months": 6}, headers=headers
            )
            if resp.status_code == 404:
                return _empty(args.parcel_id)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_get_vegetation_trend_error", exc_info=True)
        return _fail()

    if not isinstance(body, dict) or not isinstance(body.get("data_points"), list):
        logger.warning("tool_get_vegetation_trend_malformed")
        return _fail()

    try:
        points = [Point.model_validate(p) for p in body["data_points"]]
        raw_summary = body.get("summary")
        summary = Summary.model_validate(
            raw_summary if isinstance(raw_summary, dict) else {}
        )
    except ValidationError:
        logger.warning("tool_get_vegetation_trend_malformed", exc_info=True)
        return _fail()

    points = sorted((p for p in points if p.mean is not None), key=lambda p: p.date)
    if not points:
        return _empty(args.parcel_id)

    points = points[-_MAX_POINTS:]  # newest last
    return _result(
        {
            "index": "NDVI",
            "points": [p.model_dump(mode="json", exclude_none=True) for p in points],
            "summary": summary.model_dump(mode="json", exclude_none=True),
            "latest_date": points[-1].date,
        },
        args.parcel_id,
    )


GET_VEGETATION_TREND = ToolDefinition(
    name="get_vegetation_trend",
    description=(
        "NDVI of one parcel over the last six months from satellite scenes, "
        "with the date of the latest scene."
    ),
    args_model=GetVegetationTrendArgs,
    handler=get_vegetation_trend,
    required_module="vegetation-prime",
    required_setting="vegetation_url",
    timeout_s=12.0,
)
