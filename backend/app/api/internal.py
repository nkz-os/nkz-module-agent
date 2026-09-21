"""
Agent Backend - Internal Routes

Called by entity-manager and other in-cluster services — NEVER by the
browser, so these routes bypass api-gateway and carry no X-Tenant-ID /
X-User-ID headers. They authenticate via X-Internal-Service-Secret instead
(see app.middleware.verify_internal_secret).

Replace `ping` with your module's actual lifecycle endpoints (e.g.
setup-parcel, teardown) once this module participates in the parcel
activation flow — see nkz platform CLAUDE.md §2 "Module parcel activation".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.middleware import verify_internal_secret

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(verify_internal_secret)])


@router.post("/ping")
async def ping() -> dict:
    """Minimal example of an internal-secret-authenticated route."""
    return {"status": "ok"}
