"""Frozen tool contract shared by every adapter.

Tools are declared here (name, LLM description, argument schema, handler), not
spread across module repos. The AI surface stays in one replaceable place, and
it is why nothing external can put text in front of the model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, StringConstraints

from app.domain.session import SessionContext


def _no_dot_segments(value: str) -> str:
    """Reject "." and ".." between colons.

    The id is interpolated into URL paths of internal services, and an HTTP
    client normalises dot segments: "x/../data" would reach a sibling path.
    """
    if any(segment in (".", "..") for segment in value.split(":")):
        raise ValueError("parcel id must not contain dot segments")
    return value


# One reason for every adapter whose upstream answers 404 for a parcel id, so
# the model can tell a wrong id (retry with an id from a list tool) from a
# service outage.
PARCEL_NOT_FOUND = (
    "parcel not found in this holding; take parcel ids from get_parcels "
    "or get_farm_overview in this turn"
)

# Shared input surface: every tool that takes a parcel identifier validates it
# BEFORE any URL or query is built from it. Only the NGSI-LD URN form is
# accepted, with no dot segments.
ParcelId = Annotated[
    str,
    StringConstraints(pattern=r"^urn:ngsi-ld:[\w:.-]{1,120}$"),
    AfterValidator(_no_dot_segments),
]


@dataclass(frozen=True)
class Citation:
    """Where a figure came from, and when it was read."""

    source: str
    retrieved_at: datetime
    detail: str


@dataclass(frozen=True)
class ToolResult:
    """A tool's reply.

    ok=False means the caller surfaces "unavailable: <unavailable_reason>" to the
    model; the payload stays None and the reason is never invented.
    """

    ok: bool
    payload: dict | list | None
    citations: tuple[Citation, ...]
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    """One tool offered to the model."""

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[SessionContext, BaseModel], Awaitable[ToolResult]]
    required_module: str | None
    # Name of the Settings field (a service URL) this tool cannot work
    # without. An empty value means "not configured": the registry then does
    # not offer the tool at all, instead of letting the model spend a paid
    # pass to learn it is unreachable.
    required_setting: str | None = None
    # Seconds the loop waits for the handler; the loop caps it at a fixed
    # ceiling and at the turn's remaining time.
    timeout_s: float = 8.0


def available_tools() -> tuple[ToolDefinition, ...]:
    """The tools offered to the model.

    Core read-only adapters carry ``required_module=None``; module-backed
    adapters carry the module id and are filtered by the registry against
    ``public.module_installations``.
    """
    # Imported lazily: adapters import catalog (for Citation/ToolResult), so a
    # module-level import here would be a cycle. This function is the seam.
    from app.tools.adapters import (
        bioorch,
        crop_health,
        cue,
        field_ops,
        mobile_api,
        orion,
        risk,
        timeseries,
        vegetation,
        weather,
    )

    return (
        orion.GET_PARCELS,
        crop_health.GET_FARM_OVERVIEW,
        orion.GET_PARCEL_SNAPSHOT,
        timeseries.GET_TIMESERIES,
        orion.GET_ACTIVE_ALERTS,
        weather.GET_WEATHER_CONDITIONS,
        weather.GET_PARCEL_DAILY_WEATHER,
        bioorch.GET_CROP_TRIAL_EVIDENCE,
        bioorch.GET_CEREAL_MILESTONES,
        bioorch.GET_CROP_FIT,
        cue.CHECK_SUBSTANCE_AUTHORIZATION,
        mobile_api.GET_PARCEL_STATE,
        risk.GET_RISK_CATALOG,
        field_ops.GET_OPERATION_HISTORY,
        crop_health.GET_VIGOUR_ZONES,
        vegetation.GET_VEGETATION_TREND,
    )
