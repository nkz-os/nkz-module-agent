"""Crop-health adapter: intra-parcel zones whose vigour or stress differs.

Header set is the one the platform backends verify: X-Tenant-ID, X-User-ID,
X-User-Roles and X-Auth-Signature (HMAC). The route takes the short parcel id.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from nkz_platform_sdk.crypto import generate_hmac_signature
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import get_settings
from app.domain.session import SessionContext
from app.tools.catalog import Citation, ParcelId, ToolDefinition, ToolResult

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


def _empty(short_id: str) -> ToolResult:
    return _result(
        {"isWholeParcel": True, "parcel": {}, "zones": [], "note": _NO_ZONING},
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
            resp = await client.get(
                url, params={"parcelId": short_id}, headers=headers
            )
            if resp.status_code == 404:
                return _empty(short_id)
            resp.raise_for_status()
            body = resp.json()
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
    zones = [z for z in zones if z.differsFromParcel is not False]
    if not zones:
        return _empty(short_id)

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
