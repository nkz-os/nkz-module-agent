"""Mobile-api adapter: one-call parcel briefing as a read tool.

Header set is the one the platform backends verify: X-Tenant-ID, X-User-ID,
X-User-Roles and X-Auth-Signature (HMAC). The briefing route accepts a short
parcel id (or the URN); the short id is sent.
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
from app.tools.catalog import Citation, ParcelId, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15.0
_UNAVAILABLE = "parcel state unreachable"
_MAX_OPERATIONS = 10


class _Frozen(BaseModel):
    # Unknown upstream keys are ignored: the surface is exactly the fields here.
    model_config = ConfigDict(frozen=True)


class PlannedCrop(_Frozen):
    species: str | None = None
    name: str | None = None
    sowingDate: str | None = None


class Crop(_Frozen):
    status: str | None = None
    species: str | None = None
    name: str | None = None
    variety: str | None = None
    sowingDate: str | None = None
    provenance: str | None = None
    daysSinceSowing: int | None = None
    plannedCrop: PlannedCrop | None = None


class Campaign(_Frozen):
    stage: str | None = None
    progressPct: float | None = None
    deviation: float | str | None = None
    gddAccumulated: float | None = None
    dataFidelity: str | None = None


class Light(_Frozen):
    level: str | None = None
    messageKey: str | None = None


class State(_Frozen):
    water: Light | None = None
    plant: Light | None = None
    risks: Light | None = None
    overall: Light | None = None


class YieldValue(_Frozen):
    value: float | None = None
    unit: str | None = None
    low: float | None = None
    high: float | None = None


class Forecast(_Frozen):
    harvestDate: str | None = None
    # "yield" is a Python keyword: attribute yield_, wire name "yield".
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    yield_: YieldValue | None = Field(default=None, alias="yield")


class NextJob(_Frozen):
    kind: str | None = None
    when: str | None = None
    detail: Any = None


class OperationRow(_Frozen):
    type: str | None = None
    date: str | None = None
    status: str | None = None


class SourceStatus(_Frozen):
    status: str | None = None


class ParcelState(_Frozen):
    crop: Crop
    campaign: Campaign | None = None
    state: State | None = None
    assessedAt: str | None = None
    freshness: str | None = None
    forecast: Forecast | None = None
    nextJob: NextJob | None = None
    operations: tuple[OperationRow, ...] = ()
    sources: dict[str, SourceStatus] = {}


class GetParcelStateArgs(BaseModel):
    parcel_id: ParcelId


def _fail() -> ToolResult:
    return ToolResult(
        ok=False, payload=None, citations=(), unavailable_reason=_UNAVAILABLE
    )


def _source_map(raw: Any) -> dict[str, dict]:
    """Per-source status as upstream reports it. The briefing sends a plain
    string per source; a {status} object is accepted too."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            out[str(name)] = {"status": value}
        elif isinstance(value, dict):
            out[str(name)] = {"status": value.get("status")}
    return out


def _forecast(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return raw


async def get_parcel_state(
    ctx: SessionContext, args: GetParcelStateArgs
) -> ToolResult:
    settings = get_settings()
    if not settings.mobile_api_url:
        return _fail()

    short_id = args.parcel_id.rsplit(":", 1)[1]
    url = f"{settings.mobile_api_url.rstrip('/')}/api/mobile/briefing/{short_id}"
    headers = {
        "X-Tenant-ID": ctx.tenant_id,
        "X-User-ID": ctx.user_id,
        "X-User-Roles": ",".join(ctx.roles),
        "X-Auth-Signature": generate_hmac_signature(
            settings.hmac_secret, "", ctx.tenant_id
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params={"lang": "es"}, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_get_parcel_state_error", exc_info=True)
        return _fail()

    if not isinstance(body, dict) or not isinstance(body.get("crop"), dict):
        logger.warning("tool_get_parcel_state_malformed")
        return _fail()

    ops = body.get("operations")
    try:
        state = ParcelState(
            crop=body["crop"],
            campaign=body.get("campaign"),
            state=body.get("state"),
            assessedAt=(body.get("state") or {}).get("assessedAt")
            if isinstance(body.get("state"), dict)
            else None,
            freshness=(body.get("state") or {}).get("freshness")
            if isinstance(body.get("state"), dict)
            else None,
            forecast=_forecast(body.get("forecast")),
            nextJob=body.get("nextJob"),
            operations=[
                o for o in (ops if isinstance(ops, list) else []) if isinstance(o, dict)
            ][:_MAX_OPERATIONS],
            sources=_source_map(body.get("sources")),
        )
    except ValidationError:
        logger.warning("tool_get_parcel_state_malformed", exc_info=True)
        return _fail()

    return ToolResult(
        ok=True,
        payload=state.model_dump(exclude_none=True, by_alias=True),
        citations=(
            Citation(
                source="mobile-api:briefing",
                retrieved_at=datetime.now(timezone.utc),
                detail=f"parcel={short_id}",
            ),
        ),
    )


GET_PARCEL_STATE = ToolDefinition(
    name="get_parcel_state",
    description=(
        "Current state of one parcel: crop and sowing date with provenance, "
        "phenological stage, water/plant/risk lights, next job due, recent "
        "operations, harvest forecast, and which sources answered."
    ),
    args_model=GetParcelStateArgs,
    handler=get_parcel_state,
    required_module=None,
    required_setting="mobile_api_url",
    timeout_s=15.0,
)
