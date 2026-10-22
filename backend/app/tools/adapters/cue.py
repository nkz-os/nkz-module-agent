"""CUE adapter (module-backed read tool).

ROPO substance authorization via the CUE module. Matching is a case-insensitive
TOKEN match over the product's text fields — a substance that is merely a
substring of a different active ingredient (e.g. "azol" inside "tebuconazol")
never authorizes. The caller never gets an "authorized" answer without a
whole-token hit in the registry response.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, StringConstraints

from app.config import get_settings
from app.domain.session import SessionContext
from app.tools.catalog import Citation, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8.0

# CUE answers at most this many rows (LIMIT in its /productos-ropo query).
_CUE_ROW_LIMIT = 200

_SOURCE = "ROPO via CUE"

# Product text fields the substance is matched against. Frozen from the CUE
# ROPO rows (cue_api.py SELECT: nombre_comercial, ingrediente_activo).
_SUBSTANCE_TEXT_FIELDS = ("nombre_comercial", "ingrediente_activo")

_TOKEN_RE = re.compile(r"\W+", re.UNICODE)

_DISCLAIMER = (
    "Reference data from the official Spanish ROPO registry. Always verify "
    "against the product label and current local regulation."
)


class AuthorizationResult(BaseModel):
    status: Literal["authorized", "not_authorized", "unknown"]
    source: str = _SOURCE
    retrieved_at: datetime
    disclaimer: str
    detail: str | None = None


class CheckSubstanceAuthorizationArgs(BaseModel):
    substance: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    crop: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    country: str = "ES"


def _tokens(value: str) -> set[str]:
    """Lowercase whole-word tokens of a text field."""
    return {t for t in _TOKEN_RE.split(value.lower()) if t}


def _product_matches(product: object, substance_tokens: set[str]) -> bool:
    if not isinstance(product, dict):
        return False
    for field in _SUBSTANCE_TEXT_FIELDS:
        value = product.get(field)
        if isinstance(value, str) and substance_tokens.issubset(_tokens(value)):
            return True
    return False


def _tool_result(
    status: str, detail: str | None, crop: str, retrieved_at: datetime
) -> ToolResult:
    payload = AuthorizationResult(
        status=status,
        source=_SOURCE,
        retrieved_at=retrieved_at,
        disclaimer=_DISCLAIMER,
        detail=detail,
    )
    return ToolResult(
        ok=True,
        payload=payload.model_dump(),
        citations=(
            Citation(
                source="cue:productos-ropo",
                retrieved_at=retrieved_at,
                detail=f"crop={crop}",
            ),
        ),
    )


async def check_substance_authorization(
    ctx: SessionContext, args: CheckSubstanceAuthorizationArgs
) -> ToolResult:
    settings = get_settings()
    now = datetime.now(timezone.utc)

    if args.country != "ES":
        return _tool_result("unknown", "no registry for country", args.crop, now)

    substance_tokens = _tokens(args.substance)
    if not substance_tokens:
        return _tool_result(
            "not_authorized",
            f"'{args.substance}' is not a recognized substance name",
            args.crop,
            now,
        )

    if not settings.cue_api_url:
        return _tool_result("unknown", "registry unavailable", args.crop, now)

    url = f"{settings.cue_api_url.rstrip('/')}/api/modules/cue/productos-ropo"
    headers = {
        "X-Tenant-ID": ctx.tenant_id,
        "X-Internal-Service-Secret": settings.internal_service_secret,
    }
    # CUE caps every answer at _CUE_ROW_LIMIT rows, alphabetically, so a bare
    # crop listing cannot prove absence for a crop with many products. The
    # first query narrows server-side by the substance (ILIKE over name,
    # active ingredient and registration number). The longest token is sent:
    # every token of the substance must appear in a matching product, so it is
    # a necessary condition of the token match below, and it survives word
    # order differences that a full phrase would not.
    query_a = {
        "cultivo": args.crop,
        "estado": "autorizado",
        "q": max(substance_tokens, key=len),
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            body = await _fetch(client, url, query_a, headers)
            if not isinstance(body, list):
                return _tool_result(
                    "unknown", "registry response malformed", args.crop, now
                )

            if body:
                if any(_product_matches(p, substance_tokens) for p in body):
                    return _tool_result("authorized", None, args.crop, now)
                if len(body) >= _CUE_ROW_LIMIT:
                    return _tool_result(
                        "unknown", "registry result truncated", args.crop, now
                    )
                return _tool_result(
                    "not_authorized",
                    f"'{args.substance}' not found in authorized ROPO products "
                    f"for crop '{args.crop}'",
                    args.crop,
                    now,
                )

            # Nothing matched the substance. Is the crop name known at all? An
            # unmatched crop name returns an empty list too, and that must not
            # read as "not authorized".
            crop_rows = await _fetch(
                client, url, {"cultivo": args.crop, "estado": "autorizado"}, headers
            )
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_check_substance_authorization_error", exc_info=True)
        return _tool_result("unknown", "registry unreachable", args.crop, now)

    if not isinstance(crop_rows, list):
        return _tool_result("unknown", "registry response malformed", args.crop, now)
    if not crop_rows:
        return _tool_result(
            "unknown", "crop name not recognised by the registry", args.crop, now
        )
    return _tool_result(
        "not_authorized",
        f"'{args.substance}' not found in authorized ROPO products for crop "
        f"'{args.crop}'",
        args.crop,
        now,
    )


async def _fetch(
    client: httpx.AsyncClient, url: str, params: dict, headers: dict
) -> object:
    resp = await client.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return resp.json()


CHECK_SUBSTANCE_AUTHORIZATION = ToolDefinition(
    name="check_substance_authorization",
    description=(
        "Check whether a crop-protection substance is authorized for a crop in "
        "the official Spanish ROPO registry (ES only). Crop is a crop name in "
        "Spanish (e.g. 'cebada', 'trigo')."
    ),
    args_model=CheckSubstanceAuthorizationArgs,
    handler=check_substance_authorization,
    required_module="cue",
    required_setting="cue_api_url",
)
