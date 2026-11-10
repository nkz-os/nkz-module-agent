"""BioOrchestrator adapter (module-backed read tool).

The conditions evidence endpoint is tenant-free by contract: it REJECTS
``parcel_id``/``tenant_id`` with 422 (``_reject_tenant_params`` in
bioorchestrator's recommend.py). This adapter therefore never sends tenant
identifiers — the model resolves "my parcel" into conditions via
get_parcel_snapshot and passes conditions here.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.config import get_settings
from app.domain.session import SessionContext
from app.tools.catalog import Citation, ParcelId, ToolDefinition, ToolResult

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


# --- Planning tools (parcel-bound; internal-secret auth, not HMAC) ----------

_PLANNING_TIMEOUT_SECONDS = 12.0
_PLANNING_UNAVAILABLE = "bioorchestrator unreachable"
_MAX_FIT_SPECIES = 15
_MILESTONES = ("emergence", "flowering_start", "senescence_start", "maturity")


class _Frozen(BaseModel):
    # Unknown upstream keys are ignored: the surface is exactly the fields here.
    model_config = ConfigDict(frozen=True)


def _planning_fail() -> ToolResult:
    return ToolResult(
        ok=False, payload=None, citations=(), unavailable_reason=_PLANNING_UNAVAILABLE
    )


async def _planning_get(
    ctx: SessionContext, parcel_id: str, suffix: str, params: dict[str, Any] | None
) -> dict | None:
    """GET a planning route; None on any failure (never raises)."""
    settings = get_settings()
    if not settings.bioorch_api_url or not settings.internal_service_secret:
        return None
    url = (
        f"{settings.bioorch_api_url.rstrip('/')}/api/graph/planning/parcels/"
        f"{quote(parcel_id, safe='')}/{suffix}"
    )
    headers = {
        "X-Internal-Service-Secret": settings.internal_service_secret,
        "X-Tenant-ID": ctx.tenant_id,
    }
    try:
        async with httpx.AsyncClient(timeout=_PLANNING_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("tool_bioorch_planning_error suffix=%s", suffix, exc_info=True)
        return None
    return body if isinstance(body, dict) else None


def _citation(suffix: str, parcel_id: str) -> Citation:
    return Citation(
        source=f"bioorchestrator:planning/{suffix}",
        retrieved_at=datetime.now(timezone.utc),
        detail=f"parcel={parcel_id}",
    )


# get_cereal_milestones ------------------------------------------------------


class Milestone(_Frozen):
    status: str | None = None
    date: str | None = None
    date_p10: str | None = None
    date_p50: str | None = None
    date_p90: str | None = None
    gdd: float | None = None
    reason: str | None = None


class Sowing(_Frozen):
    date: str | None = None
    provenance: str | None = None


class _Season(_Frozen):
    crop: str | None = None
    sowing: Sowing | None = None
    regime: str | None = None
    as_of: str | None = None


class _Unavailable(_Frozen):
    reason: str | None = None


class _PhenologyBlock(_Frozen):
    milestones: dict[str, Milestone] = {}


class _PhenologyResponse(_Frozen):
    state: Literal["unavailable", "done", "failed"]
    unavailable: tuple[_Unavailable, ...] = ()
    season: _Season | None = None
    phenology: _PhenologyBlock | None = None


class GetCerealMilestonesArgs(BaseModel):
    parcel_id: ParcelId


async def get_cereal_milestones(
    ctx: SessionContext, args: GetCerealMilestonesArgs
) -> ToolResult:
    body = await _planning_get(ctx, args.parcel_id, "phenology", None)
    if body is None:
        return _planning_fail()
    try:
        resp = _PhenologyResponse.model_validate(body)
    except ValidationError:
        logger.warning("tool_get_cereal_milestones_malformed", exc_info=True)
        return _planning_fail()

    reason = next((u.reason for u in resp.unavailable if u.reason), None)
    if resp.state == "failed":
        return _planning_fail()
    if resp.state == "unavailable":
        # e.g. unsupported_crop: the model must learn "not a cereal", so OK.
        payload: dict[str, Any] = {"state": "unavailable"}
        if reason:
            payload["unavailable_reason"] = reason
    else:
        season = resp.season or _Season()
        milestones = (resp.phenology.milestones if resp.phenology else {}) or {}
        payload = {
            "state": "done",
            "crop": season.crop,
            "sowing": season.sowing.model_dump(mode="json", exclude_none=True)
            if season.sowing
            else None,
            "regime": season.regime,
            "as_of": season.as_of,
            "milestones": {
                name: milestones[name].model_dump(mode="json", exclude_none=True)
                for name in _MILESTONES
                if name in milestones
            },
        }
        payload = {k: v for k, v in payload.items() if v is not None}
    return ToolResult(
        ok=True,
        payload=payload,
        citations=(_citation("phenology", args.parcel_id),),
    )


GET_CEREAL_MILESTONES = ToolDefinition(
    name="get_cereal_milestones",
    description=(
        "For wheat, durum wheat and barley: sowing date with provenance and "
        "the phenological milestones reached or projected (emergence, "
        "flowering, senescence, maturity)."
    ),
    args_model=GetCerealMilestonesArgs,
    handler=get_cereal_milestones,
    required_module="bioorchestrator",
    required_setting="bioorch_api_url",
    timeout_s=12.0,
)


# get_crop_fit ---------------------------------------------------------------


class _FitReason(_Frozen):
    code: str | None = None
    gate: str | None = None


class _FitSowing(_Frozen):
    sowing_type: str | None = None
    start_month: int | None = None
    end_month: int | None = None
    typical_sowing_doy: int | None = None


class _FitIrrigation(_Frozen):
    percentiles: dict[str, float] | None = None
    unit: str | None = None


class _FitSpecies(_Frozen):
    species: str
    eppo: str | None = None
    verdict: str | None = None
    reasons: tuple[_FitReason, ...] = ()
    sowing: _FitSowing | None = None
    irrigation_need: _FitIrrigation | None = None


class _FitResponse(_Frozen):
    species: tuple[_FitSpecies, ...]


_CAMPAIGN_PATTERN = r"^\d{4}(/\d{2})?$"


class GetCropFitArgs(BaseModel):
    parcel_id: ParcelId
    # "2026" or "2026/27"; the planning route takes the start year.
    campaign: Annotated[str, StringConstraints(pattern=_CAMPAIGN_PATTERN)] | None = None


def _default_campaign_year(today: date) -> int:
    # ASSUMPTION: the agricultural campaign starts in September.
    return today.year if today.month >= 9 else today.year - 1


async def get_crop_fit(ctx: SessionContext, args: GetCropFitArgs) -> ToolResult:
    year = (
        int(args.campaign[:4])
        if args.campaign
        else _default_campaign_year(datetime.now(timezone.utc).date())
    )
    body = await _planning_get(ctx, args.parcel_id, "fit", {"campaign": year})
    if body is None:
        return _planning_fail()
    try:
        resp = _FitResponse.model_validate(body)
    except ValidationError:
        logger.warning("tool_get_crop_fit_malformed", exc_info=True)
        return _planning_fail()

    ordered = sorted(resp.species, key=lambda s: s.verdict != "fits")  # stable
    rows = [
        s.model_dump(mode="json", exclude_none=True) for s in ordered[:_MAX_FIT_SPECIES]
    ]
    return ToolResult(
        ok=True,
        payload={"campaign": year, "species": rows},
        citations=(_citation("fit", args.parcel_id),),
    )


GET_CROP_FIT = ToolDefinition(
    name="get_crop_fit",
    description=(
        "Which crops fit one parcel for a campaign and why (soil, climate, "
        "rotation gates), with suggested sowing windows."
    ),
    args_model=GetCropFitArgs,
    handler=get_crop_fit,
    required_module="bioorchestrator",
    required_setting="bioorch_api_url",
    timeout_s=12.0,
)
