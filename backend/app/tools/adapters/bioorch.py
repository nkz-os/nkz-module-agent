"""BioOrchestrator adapter (module-backed read tool).

The conditions evidence endpoint is tenant-free by contract: it REJECTS
``parcel_id``/``tenant_id`` with 422 (``_reject_tenant_params`` in
bioorchestrator's recommend.py). This adapter therefore never sends tenant
identifiers — the model resolves "my parcel" into conditions via
get_parcel_snapshot and passes conditions here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, Field, StringConstraints, ValidationError

from app.config import get_settings
from app.domain.session import SessionContext
from app.tools.catalog import Citation, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8.0

# Input validation frozen from bioorchestrator recommend.py (the real
# endpoint contract): crop is an EPPO/alpha-numeric code, climate a Köppen
# class, water_regime secano|regadío. Mirroring them here means a 422 can no
# longer be caused by this adapter. (country is deliberately absent: the
# endpoint only applies it on tier=regional, which this tool never requests,
# so exposing it would promise a filter that does nothing.)
_EPPO_PATTERN = r"^[A-Za-z0-9]{2,6}$"
_KOPPEN_PATTERN = r"^[A-Z][A-Za-z]{0,2}$"

# Response contract frozen from bioorchestrator recommend.py:
#   list_trial_evidence -> {items, total, page, page_size, purpose, tier,
#                           [zone_pool]} then attach_attributions adds
#   attributions. Each item is the distinct-trial projection in list_trial_evidence.


class Attribution(BaseModel):
    source_id: str
    text: str
    url: str
    licence_id: str
    licence_url: str
    processing_note: dict[str, str] | None = None


class TrialEvidenceItem(BaseModel):
    trial_id: str | None = None
    variety: str | None = None
    site: str | None = None
    year: int | None = None
    yield_kg_ha: float | None = None
    irrigation_regime: str | None = None
    production_system: str | None = None
    source_id: str | None = None
    confidence: str | None = None
    tier: str | None = None
    basis: str | None = None
    irrigation_status: str | None = None


class TrialEvidence(BaseModel):
    items: list[TrialEvidenceItem]
    total: int
    page: int
    page_size: int
    purpose: str
    tier: str
    zone_pool: str | None = None
    attributions: list[Attribution] = Field(default_factory=list)


class GetCropTrialEvidenceArgs(BaseModel):
    # climate is REQUIRED here because the real endpoint rejects the query
    # without climate_class (recommend.py); an optional field would build a
    # request guaranteed to 422.
    crop: Annotated[str, StringConstraints(pattern=_EPPO_PATTERN)]
    climate: Annotated[str, StringConstraints(pattern=_KOPPEN_PATTERN)]
    water_regime: Literal["secano", "regadío"] | None = None


def _params(args: GetCropTrialEvidenceArgs) -> dict[str, str]:
    """The tenant-free query. parcel_id/tenant_id are never added here."""
    params: dict[str, str] = {"crop": args.crop, "climate_class": args.climate}
    if args.water_regime:
        params["irrigation_regime"] = args.water_regime
    return params


async def get_crop_trial_evidence(
    ctx: SessionContext, args: GetCropTrialEvidenceArgs
) -> ToolResult:
    settings = get_settings()
    if not settings.bioorch_api_url:
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="advisory unavailable",
        )

    url = (
        f"{settings.bioorch_api_url.rstrip('/')}"
        "/api/graph/agriculture/recommend/evidence"
    )
    params = _params(args)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_get_crop_trial_evidence_error", exc_info=True)
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="advisory unavailable",
        )

    if not isinstance(body, dict):
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="advisory unavailable",
        )

    try:
        evidence = TrialEvidence.model_validate(body)
    except ValidationError:
        logger.warning("tool_get_crop_trial_evidence_malformed", exc_info=True)
        return ToolResult(
            ok=False,
            payload=None,
            citations=(),
            unavailable_reason="advisory unavailable",
        )

    return ToolResult(
        ok=True,
        payload=evidence.model_dump(),
        citations=(
            Citation(
                source="bioorchestrator:recommend/evidence",
                retrieved_at=datetime.now(timezone.utc),
                detail=f"crop={args.crop}",
            ),
        ),
    )


GET_CROP_TRIAL_EVIDENCE = ToolDefinition(
    name="get_crop_trial_evidence",
    description=(
        "Get crop trial evidence for a crop under explicit conditions (EPPO "
        "crop code, e.g. TRZAX; climate class; irrigation regime), "
        "with provenance. Conditions-only: it never accepts a parcel or "
        "tenant id."
    ),
    args_model=GetCropTrialEvidenceArgs,
    handler=get_crop_trial_evidence,
    required_module="bioorchestrator",
    required_setting="bioorch_api_url",
)
