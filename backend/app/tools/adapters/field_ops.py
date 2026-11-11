"""Field-operations adapter: dated record of what was done on a parcel.

Header set is the one the platform backends verify: X-Tenant-ID, X-User-ID,
X-User-Roles and X-Auth-Signature (HMAC).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

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

_TIMEOUT_SECONDS = 8.0
_UNAVAILABLE = "operation history unreachable"
_UPSTREAM_LIMIT = 100

OperationType = Literal[
    "sowing",
    "irrigation",
    "fertilization",
    "spraying",
    "tillage",
    "harvesting",
    "haymaking",
    "baling",
    "scouting",
]

_DATE_FIELDS = ("endedAt", "startedAt", "plannedDate", "dateCreated")
_PRODUCT_FIELDS = ("productName", "fertilizerType", "variety", "tillageType")
# (field, unit as the module stores it). Order is the precedence when a record
# carries more than one.
_RATE_FIELDS = (
    ("productRate", "L/ha"),
    ("fertilizerRate", "kg/ha"),
    ("waterPerHectare", "L/ha"),
    ("seedingRate", "kg/ha"),
    ("harvestedWeight", "kg"),
)


class OperationRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    type: str
    status: str | None = None
    date: str | None = None
    product: str | None = None
    rate: float | None = None
    rate_unit: str | None = None


class OperationHistory(BaseModel):
    model_config = ConfigDict(frozen=True)

    operations: tuple[OperationRow, ...]
    truncated: bool


class GetOperationHistoryArgs(BaseModel):
    parcel_id: ParcelId
    operation_type: OperationType | None = None
    limit: int = Field(20, ge=1, le=50)


def _fail() -> ToolResult:
    return ToolResult(
        ok=False, payload=None, citations=(), unavailable_reason=_UNAVAILABLE
    )


def _text(raw: Any) -> str | None:
    if isinstance(raw, dict):  # typed {"@type", "@value"} or {"value": ...}
        raw = raw.get("@value", raw.get("value"))
    return raw if isinstance(raw, str) and raw else None


def _number(raw: Any) -> float | None:
    if isinstance(raw, dict):
        raw = raw.get("value")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _when(row: dict) -> str | None:
    for key in _DATE_FIELDS:
        value = _text(row.get(key))
        if value:
            return value
    return None


def _sort_key(date: str | None) -> tuple[int, float]:
    """Newest first once reversed; undated or unparseable rows sort last."""
    if not date:
        return (0, 0.0)
    try:
        parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        return (0, 0.0)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (1, parsed.timestamp())


def _row(raw: dict) -> OperationRow | None:
    oid, kind = _text(raw.get("id")), _text(raw.get("operationType"))
    if not oid or not kind:
        return None
    product = next(
        (t for t in (_text(raw.get(f)) for f in _PRODUCT_FIELDS) if t), None
    )
    rate, unit = None, None
    for field, field_unit in _RATE_FIELDS:
        value = _number(raw.get(field))
        if value is not None:
            rate, unit = value, field_unit
            break
    return OperationRow(
        id=oid,
        type=kind,
        status=_text(raw.get("status")),
        date=_when(raw),
        product=product,
        rate=rate,
        rate_unit=unit,
    )


async def get_operation_history(
    ctx: SessionContext, args: GetOperationHistoryArgs
) -> ToolResult:
    settings = get_settings()
    if not settings.field_ops_url:
        return _fail()

    url = f"{settings.field_ops_url.rstrip('/')}/api/field-operations/operations"
    headers = {
        "X-Tenant-ID": ctx.tenant_id,
        "X-User-ID": ctx.user_id,
        "X-User-Roles": ",".join(ctx.roles),
        "X-Auth-Signature": generate_hmac_signature(
            settings.hmac_secret, "", ctx.tenant_id
        ),
    }
    params: dict[str, Any] = {"parcel_id": args.parcel_id, "limit": _UPSTREAM_LIMIT}
    if args.operation_type:
        params["operation_type"] = args.operation_type

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return parcel_not_found()
        logger.warning("tool_get_operation_history_error", exc_info=True)
        return _fail()
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_get_operation_history_error", exc_info=True)
        return _fail()

    raw_rows = body.get("operations") if isinstance(body, dict) else None
    if not isinstance(raw_rows, list):
        logger.warning("tool_get_operation_history_malformed")
        return _fail()

    try:
        rows = [r for r in (_row(x) for x in raw_rows if isinstance(x, dict)) if r]
        rows.sort(key=lambda r: _sort_key(r.date), reverse=True)
        count = body.get("count")
        history = OperationHistory(
            operations=tuple(rows[: args.limit]),
            truncated=len(rows) > args.limit
            or (count if isinstance(count, int) else len(raw_rows))
            >= _UPSTREAM_LIMIT,
        )
    except ValidationError:
        logger.warning("tool_get_operation_history_malformed", exc_info=True)
        return _fail()

    return ToolResult(
        ok=True,
        payload=history.model_dump(mode="json", exclude_none=True),
        citations=(
            Citation(
                source="field-operations:operations",
                retrieved_at=datetime.now(timezone.utc),
                detail=(
                    f"parcel={args.parcel_id} type={args.operation_type or 'any'} "
                    f"limit={args.limit}"
                ),
            ),
        ),
    )


GET_OPERATION_HISTORY = ToolDefinition(
    name="get_operation_history",
    description=(
        "Dated record of what was done on one parcel (sowing, irrigation, "
        "fertilisation, spraying, tillage, harvest, haymaking, baling, "
        "scouting), newest first; says if the list was truncated."
    ),
    args_model=GetOperationHistoryArgs,
    handler=get_operation_history,
    required_module="field-operations",
    required_setting="field_ops_url",
)
