"""Orion-LD read adapters.

Tenant isolation is the whole point here: the adapter is blind to tenant. It
only reads ``ctx.tenant_id`` and hands it to ``OrionClient``, which injects the
NGSILD-Tenant / Fiware-Service headers. No tool signature accepts a tenant_id.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from nkz_platform_sdk.orion import OrionClient
from pydantic import BaseModel

from app.domain.session import SessionContext
from app.tools.catalog import Citation, ParcelId, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


class ParcelSummary(BaseModel):
    """Frozen from the entity-manager AgriParcel writer plus a live sample.

    Every attribute except the id is optional: the writer sets them only when
    the parcel was created with them. No geometry here -- a full polygon per
    parcel floods the payload and the model has no use for it.
    """

    id: str
    name: str | None = None
    category: str | None = None
    area: float | None = None
    irrigationRegime: str | None = None
    cropSeasonStart: str | None = None
    cropSeasonEnd: str | None = None
    crop: str | None = None
    cropVariety: str | None = None
    lastPlantedAt: str | None = None
    cropType: str | None = None
    municipality: str | None = None
    province: str | None = None


class Centroid(BaseModel):
    lon: float
    lat: float


class ParcelSnapshot(ParcelSummary):
    elevation: float | None = None
    terrainSlope: float | None = None
    terrainAspect: float | None = None
    timeZone: str | None = None
    ndviEnabled: bool | None = None
    centroid: Centroid | None = None


class AlertSummary(BaseModel):
    id: str
    category: str | None
    alertType: str | None
    severity: str | None
    status: str | None
    observedAt: str | None
    refEntity: str | None


class GetParcelsArgs(BaseModel):
    """get_parcels takes no arguments."""


class GetParcelSnapshotArgs(BaseModel):
    parcel_id: ParcelId


class GetActiveAlertsArgs(BaseModel):
    """get_active_alerts takes no arguments."""


# The Alert surface is frozen from nkz-module-risk/backend/app/alerts/contract.py:
# category, alertType, severity, status, observedAt and refEntity — and only
# those. A missing NGSI-LD attribute becomes None, never a free dict.


def _value(attr: Any) -> Any:
    """Unwrap a normalized NGSI-LD attribute ({"type", "value"}) or pass through."""
    if isinstance(attr, dict):
        return attr.get("value")
    return attr


def _alert_attr(entity: dict, key: str) -> str | None:
    """Read one frozen Alert attribute as text, unwrapping DateTime @value."""
    raw = _value(entity.get(key))
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("@value", raw)
    return _as_str(raw)


def _ref_attr(entity: dict, key: str) -> str | None:
    """Read one frozen Alert Relationship attribute (refEntity.object)."""
    raw = entity.get(key)
    if isinstance(raw, dict):
        raw = raw.get("object")
    return _as_str(raw)


def _alert_summary(entity: dict) -> AlertSummary:
    return AlertSummary(
        id=entity["id"],
        category=_alert_attr(entity, "category"),
        alertType=_alert_attr(entity, "alertType"),
        severity=_alert_attr(entity, "severity"),
        status=_alert_attr(entity, "status"),
        observedAt=_alert_attr(entity, "observedAt"),
        refEntity=_ref_attr(entity, "refEntity"),
    )


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scalar(entity: dict, key: str) -> Any:
    """A Property value, unwrapping a typed {"@type", "@value"} (DateTime)."""
    raw = _value(entity.get(key))
    if isinstance(raw, dict):
        raw = raw.get("@value")
    return raw


def _relationship(entity: dict, *keys: str) -> str | None:
    """Object URN of the first relationship present among ``keys``.

    The crop relationship is read under both its current and legacy name
    (platform migration rule).
    """
    for key in keys:
        raw = entity.get(key)
        if isinstance(raw, dict):
            raw = raw.get("object")
        if isinstance(raw, str) and raw:
            return raw
    return None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _summary_fields(entity: dict) -> dict[str, Any]:
    return {
        "id": entity["id"],
        "name": _as_str(_scalar(entity, "name")),
        "category": _as_str(_scalar(entity, "category")),
        "area": _as_float(_scalar(entity, "area")),
        "irrigationRegime": _as_str(_scalar(entity, "irrigationRegime")),
        "cropSeasonStart": _as_str(_scalar(entity, "cropSeasonStart")),
        "cropSeasonEnd": _as_str(_scalar(entity, "cropSeasonEnd")),
        "crop": _relationship(entity, "hasAgriCrop", "refAgriCrop"),
        "cropVariety": _relationship(entity, "hasAgriCropVariety"),
        "lastPlantedAt": _as_str(_scalar(entity, "lastPlantedAt")),
        "cropType": _as_str(_scalar(entity, "cropType")),
        "municipality": _as_str(_scalar(entity, "municipality")),
        "province": _as_str(_scalar(entity, "province")),
    }


def _summary(entity: dict) -> ParcelSummary:
    return ParcelSummary(**_summary_fields(entity))


def _centroid(entity: dict) -> Centroid | None:
    """Mean of the outer-ring vertices of a Polygon/MultiPolygon, 4 decimals.

    Only this point reaches the model, never the polygon itself.
    """
    geom = _value(entity.get("location"))
    if not isinstance(geom, dict):
        return None
    coords = geom.get("coordinates")
    kind = geom.get("type")
    try:
        if kind == "Polygon":
            rings = [coords[0]]
        elif kind == "MultiPolygon":
            rings = [polygon[0] for polygon in coords]
        else:
            return None
        points = [
            (float(p[0]), float(p[1]))
            for ring in rings
            for p in (ring[:-1] if len(ring) > 1 else ring)
        ]
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not points:
        return None
    return Centroid(
        lon=round(sum(p[0] for p in points) / len(points), 4),
        lat=round(sum(p[1] for p in points) / len(points), 4),
    )


def _snapshot(entity: dict) -> ParcelSnapshot:
    return ParcelSnapshot(
        **_summary_fields(entity),
        elevation=_as_float(_scalar(entity, "elevation")),
        terrainSlope=_as_float(_scalar(entity, "terrainSlope")),
        terrainAspect=_as_float(_scalar(entity, "terrainAspect")),
        timeZone=_as_str(_scalar(entity, "timeZone")),
        ndviEnabled=_as_bool(_scalar(entity, "ndviEnabled")),
        centroid=_centroid(entity),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_parcels(ctx: SessionContext, args: GetParcelsArgs) -> ToolResult:
    client = OrionClient(ctx.tenant_id)
    try:
        entities = await client.query_entities(type="AgriParcel", limit=50)
        parcels = [_summary(e) for e in entities]
    except Exception:
        # Operator signal only: the model-facing reason stays generic, and the
        # exc_info never reaches the LLM or the user.
        logger.warning("tool_get_parcels_orion_error", exc_info=True)
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="parcel list unavailable",
        )
    finally:
        await client.close()
    return ToolResult(
        ok=True,
        payload=[p.model_dump(exclude_none=True) for p in parcels],
        citations=(
            Citation(
                source="orion:AgriParcel",
                retrieved_at=_now(),
                detail="type=AgriParcel limit=50",
            ),
        ),
    )


async def get_active_alerts(
    ctx: SessionContext, args: GetActiveAlertsArgs
) -> ToolResult:
    client = OrionClient(ctx.tenant_id)
    try:
        entities = await client.query_entities(type="Alert", limit=50)
        alerts = [
            a
            for a in (_alert_summary(e) for e in entities)
            if a.status == "active"
        ]
    except Exception:
        logger.warning("tool_get_active_alerts_orion_error", exc_info=True)
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="alert list unavailable",
        )
    finally:
        await client.close()
    return ToolResult(
        ok=True,
        payload=[a.model_dump() for a in alerts],
        citations=(
            Citation(
                source="orion:Alert",
                retrieved_at=_now(),
                detail="type=Alert limit=50",
            ),
        ),
    )


async def get_parcel_snapshot(
    ctx: SessionContext, args: GetParcelSnapshotArgs
) -> ToolResult:
    client = OrionClient(ctx.tenant_id)
    try:
        entity = await client.get_entity(args.parcel_id)
    except Exception:
        logger.warning("tool_get_parcel_snapshot_orion_error", exc_info=True)
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="parcel lookup unavailable",
        )
    finally:
        await client.close()
    if not isinstance(entity, dict):
        # get_entity returning nothing means the URN simply is not there —
        # typically a parcel id the model guessed. That is an honest
        # "not found", not an error worth a warning.
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="parcel not found",
        )
    snapshot = _snapshot(entity)
    return ToolResult(
        ok=True,
        payload=snapshot.model_dump(exclude_none=True),
        citations=(
            Citation(
                source="orion:AgriParcel",
                retrieved_at=_now(),
                detail=args.parcel_id,
            ),
        ),
    )


GET_ACTIVE_ALERTS = ToolDefinition(
    name="get_active_alerts",
    description=(
        "List the current tenant's active Alert entities (id, category, "
        "alertType, severity, status, observedAt, refEntity), up to 50."
    ),
    args_model=GetActiveAlertsArgs,
    handler=get_active_alerts,
    required_module="risk",
)

GET_PARCELS = ToolDefinition(
    name="get_parcels",
    description=(
        "List the current tenant's AgriParcel entities (id, name, category, "
        "area, irrigation regime, crop season, crop and variety URNs), up to "
        "50. Every field except the id may be missing."
    ),
    args_model=GetParcelsArgs,
    handler=get_parcels,
    required_module=None,
)

GET_PARCEL_SNAPSHOT = ToolDefinition(
    name="get_parcel_snapshot",
    description=(
        "Read one AgriParcel entity by URN: the parcel summary plus "
        "elevation, terrain slope and aspect, time zone, NDVI flag and the "
        "parcel centroid (lon/lat). Fields may be missing."
    ),
    args_model=GetParcelSnapshotArgs,
    handler=get_parcel_snapshot,
    required_module=None,
)
