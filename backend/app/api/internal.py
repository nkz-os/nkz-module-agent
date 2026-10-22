"""
Agent Backend - Internal Routes

Called by entity-manager and other in-cluster services — NEVER by the
browser, so these routes bypass api-gateway and carry no X-Tenant-ID /
X-User-ID headers. They authenticate via X-Internal-Service-Secret instead
(see app.middleware.verify_internal_secret).

Version 1 of this module is read-only and does not participate in the
parcel activation flow (no per-parcel setup/teardown to run) — see the
README. `ping` is a real, live route: it lets an operator or another
in-cluster service confirm the shared-secret auth path is wired correctly
without needing a parcel-scoped endpoint to exist yet. Add lifecycle routes
here (e.g. setup-parcel, teardown — see nkz platform CLAUDE.md §2 "Module
parcel activation") if this module ever needs them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.middleware import verify_internal_secret

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(verify_internal_secret)])


@router.post("/ping")
async def ping() -> dict:
    """Confirm the X-Internal-Service-Secret auth path is wired correctly."""
    return {"status": "ok"}
