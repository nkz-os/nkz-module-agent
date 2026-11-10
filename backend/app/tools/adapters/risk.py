"""Risk adapter: the catalog of alert types the risk module can raise.

Header set is the one the platform backends verify: X-Tenant-ID, X-User-ID,
X-User-Roles and X-Auth-Signature (HMAC).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from nkz_platform_sdk.crypto import generate_hmac_signature
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import get_settings
from app.domain.session import SessionContext
from app.tools.catalog import Citation, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8.0
_UNAVAILABLE = "risk catalog unreachable"


class RiskType(BaseModel):
    model_config = ConfigDict(frozen=True)

    alertType: str
    category: str | None = None
    applicable_crop_groups: tuple[str, ...] = ()


class GetRiskCatalogArgs(BaseModel):
    """get_risk_catalog takes no arguments."""


def _fail() -> ToolResult:
    return ToolResult(
        ok=False, payload=None, citations=(), unavailable_reason=_UNAVAILABLE
    )


async def get_risk_catalog(
    ctx: SessionContext, args: GetRiskCatalogArgs
) -> ToolResult:
    settings = get_settings()
    if not settings.risk_url:
        return _fail()

    url = f"{settings.risk_url.rstrip('/')}/api/risk/catalog"
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
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_get_risk_catalog_error", exc_info=True)
        return _fail()

    if not isinstance(body, list):
        logger.warning("tool_get_risk_catalog_malformed")
        return _fail()

    try:
        rows = [
            RiskType(
                alertType=row["alert_type"],
                category=row.get("category"),
                applicable_crop_groups=row.get("applicable_crop_groups") or (),
            )
            for row in body
            if isinstance(row, dict) and isinstance(row.get("alert_type"), str)
        ]
    except ValidationError:
        logger.warning("tool_get_risk_catalog_malformed", exc_info=True)
        return _fail()

    return ToolResult(
        ok=True,
        payload=[r.model_dump(mode="json", exclude_none=True) for r in rows],
        citations=(
            Citation(
                source="risk:catalog",
                retrieved_at=datetime.now(timezone.utc),
                detail="active alert types",
            ),
        ),
    )


GET_RISK_CATALOG = ToolDefinition(
    name="get_risk_catalog",
    description=(
        "List the kinds of risk alert the platform can raise (alertType, "
        "category and the crop groups each applies to; an empty list means "
        "all crops). Use it to say which risks are watched for a crop."
    ),
    args_model=GetRiskCatalogArgs,
    handler=get_risk_catalog,
    required_module="risk",
    required_setting="risk_url",
)
