"""Crop-health adapter: intra-parcel vigour zones and the whole-farm overview.

Header set is the one the platform backends verify: X-Tenant-ID, X-User-ID,
X-User-Roles and X-Auth-Signature (HMAC). The route takes the short parcel id.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from nkz_platform_sdk.crypto import generate_hmac_signature
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import get_settings
from app.domain.session import SessionContext
from app.tools.catalog import (
    Citation,
    ParcelId,
    ToolDefinition,
    ToolResult,
    parcel_not_found,
)

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 12.0
_UNAVAILABLE = "vigour zones unreachable"
_NO_ZONING = "No zoning: the parcel is assessed as a whole."


class _Frozen(BaseModel):
    # Unknown upstream keys (geometry, urns, ...) are ignored.
    model_config = ConfigDict(frozen=True)


class ParcelValues(_Frozen):
    vigorIndex: float | None = None
    vigorCondition: str | None = None
    cwsiValue: float | None = None
    compositeStressIndex: float | None = None
    overallSeverity: str | None = None


class Zone(_Frozen):
    zoneId: str
    zoneRank: int | None = None
    vigorIndex: float | None = None
    vigorCondition: str | None = None
    cwsiValue: float | None = None
    compositeStressIndex: float | None = None
    overallSeverity: str | None = None
    wetnessClass: str | None = None
    inputFidelity: dict[str, Any] = {}
    differsFromParcel: bool | None = None


class GetVigourZonesArgs(BaseModel):
    parcel_id: ParcelId


def _headers(ctx: SessionContext, settings: Any) -> dict[str, str]:
    return {
        "X-Tenant-ID": ctx.tenant_id,
        "X-User-ID": ctx.user_id,
        "X-User-Roles": ",".join(ctx.roles),
        "X-Auth-Signature": generate_hmac_signature(
            settings.hmac_secret, "", ctx.tenant_id
        ),
    }


def _fail() -> ToolResult:
    return ToolResult(
        ok=False, payload=None, citations=(), unavailable_reason=_UNAVAILABLE
    )


def _result(payload: dict, short_id: str) -> ToolResult:
    return ToolResult(
        ok=True,
        payload=payload,
        citations=(
            Citation(
                source="crop-health:assessments/zones",
                retrieved_at=datetime.now(timezone.utc),
                detail=f"parcel={short_id}",
            ),
        ),
    )


_NONE_DIFFER = "Zones exist; none differs from the parcel."


def _empty(short_id: str, note: str = _NO_ZONING) -> ToolResult:
    return _result(
        {"isWholeParcel": True, "parcel": {}, "zones": [], "note": note},
        short_id,
    )


async def get_vigour_zones(
    ctx: SessionContext, args: GetVigourZonesArgs
) -> ToolResult:
    settings = get_settings()
    if not settings.crop_health_url:
        return _fail()

    short_id = args.parcel_id.rsplit(":", 1)[1]
    url = f"{settings.crop_health_url.rstrip('/')}/api/crop-health/assessments/zones"
    headers = _headers(ctx, settings)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url, params={"parcelId": short_id}, headers=headers
            )
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return parcel_not_found()
        logger.warning("tool_get_vigour_zones_error", exc_info=True)
        return _fail()
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_get_vigour_zones_error", exc_info=True)
        return _fail()

    if not isinstance(body, dict) or not isinstance(body.get("zones", []), list):
        logger.warning("tool_get_vigour_zones_malformed")
        return _fail()

    if body.get("isWholeParcel") is True:
        return _empty(short_id)

    raw_parcel = body.get("parcelValues")
    if not isinstance(raw_parcel, dict):
        raw_parcel = body.get("parcel")
    try:
        parcel = ParcelValues.model_validate(
            raw_parcel if isinstance(raw_parcel, dict) else {}
        )
        zones = [Zone.model_validate(z) for z in body.get("zones", [])]
    except ValidationError:
        logger.warning("tool_get_vigour_zones_malformed", exc_info=True)
        return _fail()

    # Only zones flagged as differing are the point of the tool; a zone with no
    # flag (older producer) is kept.
    had_zones = bool(zones)
    zones = [z for z in zones if z.differsFromParcel is not False]
    if not zones:
        return _empty(short_id, _NONE_DIFFER if had_zones else _NO_ZONING)

    zones.sort(key=lambda z: (z.zoneRank is None, z.zoneRank or 0, z.zoneId))
    return _result(
        {
            "isWholeParcel": False,
            "parcel": parcel.model_dump(mode="json", exclude_none=True),
            "zones": [
                z.model_dump(
                    mode="json", exclude_none=True, exclude={"differsFromParcel"}
                )
                for z in zones
            ],
        },
        short_id,
    )


GET_VIGOUR_ZONES = ToolDefinition(
    name="get_vigour_zones",
    description=(
        "Intra-parcel zones whose vigour or stress differs from the parcel "
        "average, ranked worst first."
    ),
    args_model=GetVigourZonesArgs,
    handler=get_vigour_zones,
    required_module="crop-health",
    required_setting="crop_health_url",
    timeout_s=12.0,
)


_OVERVIEW_UNAVAILABLE = "crop health unreachable"
# The 8000-char fence on a tool result (app.agent.prompt.MAX_PAYLOAD) is the
# effective bound; this cap only keeps the list well inside it.
_OVERVIEW_CAP = 40
_URN_PREFIX = "urn:ngsi-ld:"
_PARCEL_URN_PREFIX = "urn:ngsi-ld:AgriParcel:"


class _UpstreamNextJob(_Frozen):
    kind: str
    when: str | None = None
    detail: Any = None


class _UpstreamParcel(_Frozen):
    parcelId: str
    parcelName: str | None = None
    cropName: str | None = None
    phenologyStage: str | None = None
    areaHa: float | None = None
    overallSeverity: str | None = None
    cwsiValue: float | None = None
    vigorIndex: float | None = None
    assessedAt: str | None = None
    hasData: bool = False
    nextJob: _UpstreamNextJob | None = None


class _UpstreamSummary(_Frozen):
    surprises: int = 0
    jobsToday: int = 0
    noData: int = 0


class _UpstreamOverview(_Frozen):
    parcels: list[_UpstreamParcel]
    summary: _UpstreamSummary = Field(default_factory=_UpstreamSummary)


class NextJob(_Frozen):
    kind: str
    when: str | None = None
    detail: Any = None


class OverviewParcel(_Frozen):
    parcel_id: str
    name: str | None = None
    crop: str | None = None
    stage: str | None = None
    area_ha: float | None = None
    severity: str | None = None
    cwsi: float | None = None
    vigor: float | None = None
    assessed_at: str | None = None
    has_data: bool = False
    next_job: NextJob | None = None


class OverviewSummary(_Frozen):
    surprises: int = 0
    jobs_today: int = 0
    no_data: int = 0


class FarmOverview(_Frozen):
    parcels: list[OverviewParcel]
    summary: OverviewSummary
    truncated: bool = False


class GetFarmOverviewArgs(BaseModel):
    pass


def _overview_fail() -> ToolResult:
    return ToolResult(
        ok=False, payload=None, citations=(), unavailable_reason=_OVERVIEW_UNAVAILABLE
    )


def _map_parcel(p: _UpstreamParcel) -> OverviewParcel:
    job = p.nextJob
    return OverviewParcel(
        parcel_id=(
            p.parcelId
            if p.parcelId.startswith(_URN_PREFIX)
            else f"{_PARCEL_URN_PREFIX}{p.parcelId}"
        ),
        name=p.parcelName,
        crop=p.cropName,
        stage=p.phenologyStage,
        area_ha=p.areaHa,
        severity=p.overallSeverity,
        cwsi=p.cwsiValue,
        vigor=p.vigorIndex,
        assessed_at=p.assessedAt,
        has_data=p.hasData,
        next_job=(
            NextJob(kind=job.kind, when=job.when, detail=job.detail or None)
            if job is not None
            else None
        ),
    )


async def get_farm_overview(
    ctx: SessionContext, args: GetFarmOverviewArgs
) -> ToolResult:
    settings = get_settings()
    if not settings.crop_health_url:
        return _overview_fail()

    url = f"{settings.crop_health_url.rstrip('/')}/api/crop-health/parcels"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=_headers(ctx, settings))
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_get_farm_overview_error", exc_info=True)
        return _overview_fail()

    try:
        upstream = _UpstreamOverview.model_validate(body)
        overview = FarmOverview(
            parcels=[_map_parcel(p) for p in upstream.parcels[:_OVERVIEW_CAP]],
            summary=OverviewSummary(
                surprises=upstream.summary.surprises,
                jobs_today=upstream.summary.jobsToday,
                no_data=upstream.summary.noData,
            ),
            truncated=len(upstream.parcels) > _OVERVIEW_CAP,
        )
    except ValidationError:
        logger.warning("tool_get_farm_overview_malformed", exc_info=True)
        return _overview_fail()

    return ToolResult(
        ok=True,
        payload=overview.model_dump(mode="json", exclude_none=True),
        citations=(
            Citation(
                source="crop-health:parcels",
                retrieved_at=datetime.now(timezone.utc),
                detail="tenant work list",
            ),
        ),
    )


GET_FARM_OVERVIEW = ToolDefinition(
    name="get_farm_overview",
    description=(
        "One-call overview of every parcel of the holding: name, crop, "
        "phenological stage, overall severity, water stress (CWSI), vigour, "
        "assessment date and the next job due. Use this for whole-farm "
        "questions instead of reading parcels one by one."
    ),
    args_model=GetFarmOverviewArgs,
    handler=get_farm_overview,
    required_module="crop-health",
    required_setting="crop_health_url",
    timeout_s=15.0,
)
