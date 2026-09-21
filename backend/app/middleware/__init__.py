"""
Agent Backend - Authentication

Trusts gateway-injected headers (X-Tenant-ID, X-User-ID, X-User-Roles) set by
api-gateway once it has already authenticated the request against Keycloak.

Module backends do NOT validate JWTs or fetch JWKS directly — see the
platform rule: "Rely on api-gateway headers (X-Tenant-ID, X-User-ID,
X-User-Roles) via require_auth(). Do NOT implement JWKS/JWT validation in
module backends." This file delegates to nkz_platform_sdk.auth.require_auth,
the canonical implementation (defense-in-depth X-Auth-Signature HMAC check
when HMAC_SECRET/REQUIRE_HMAC_SIGNATURE are configured) — do not hand-roll
gateway-header parsing or invent an alternative auth format here.

Tenant source is X-Tenant-ID — NOT Fiware-Service (that header is for direct
Orion-LD calls, a different concern; see nkz_platform_sdk.orion.OrionClient).

/internal/* routes are a SEPARATE mechanism: they are called directly by
entity-manager and other in-cluster services, bypassing api-gateway, so there
are no gateway headers to trust. They authenticate via verify_internal_secret()
below (X-Internal-Service-Secret, hmac.compare_digest, constant-time) — apply
it as a route dependency, it is not a blanket path-prefix exemption.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from nkz_platform_sdk.auth import AuthContext, require_auth as _sdk_require_auth

from app.config import Settings, get_settings

__all__ = [
    "AuthContext",
    "TokenPayload",
    "require_auth",
    "get_current_user",
    "get_tenant_id",
    "require_roles",
    "verify_internal_secret",
]

# Backward-compat alias for route code written against the old TokenPayload name.
TokenPayload = AuthContext


def require_auth(roles: Optional[list[str]] = None):
    """FastAPI dependency for user-facing endpoints.

    Reads X-Tenant-ID / X-User-ID / X-User-Roles injected by api-gateway.
    Optionally enforces role membership.

    Usage:
        @router.get("/data")
        async def list_data(auth: AuthContext = require_auth()):
            return {"tenant": auth.tenant_id}
    """
    return _sdk_require_auth(roles=roles)


# Module-level singleton dependency so `Depends(get_current_user)` /
# `Depends(get_tenant_id)` / `Depends(require_roles(...))` keep working as
# plain callables (FastAPI resolves nested Depends() defaults automatically).
_default_auth_dep = _sdk_require_auth()


def get_current_user(auth: AuthContext = _default_auth_dep) -> AuthContext:
    """FastAPI dependency: authenticated request context (gateway headers).

    Usage:
        @router.get("/data")
        async def list_data(user: AuthContext = Depends(get_current_user)):
            ...
    """
    return auth


def get_tenant_id(auth: AuthContext = _default_auth_dep) -> str:
    """FastAPI dependency: tenant ID resolved from X-Tenant-ID.

    Usage:
        @router.get("/data")
        async def list_data(tenant_id: str = Depends(get_tenant_id)):
            ...
    """
    return auth.tenant_id


def require_roles(*required_roles: str):
    """FastAPI dependency factory: require the caller to hold one of the given roles.

    Usage:
        @router.get("/admin-only")
        async def admin_route(user: AuthContext = Depends(require_roles("PlatformAdmin"))):
            ...
    """
    role_dep = _sdk_require_auth(roles=list(required_roles))

    def _check(auth: AuthContext = role_dep) -> AuthContext:
        return auth

    return _check


async def verify_internal_secret(
    x_internal_service_secret: Optional[str] = Header(None, alias="X-Internal-Service-Secret"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate /internal/* routes via the shared K8s secret.

    Constant-time comparison (hmac.compare_digest) — NOT the api-gateway's
    X-Auth-Signature HMAC format (that one is user-bound, gateway->backend
    proxy hop only). Internal service-to-service calls (e.g. entity-manager
    calling this module's setup/lifecycle endpoints) use this shared secret
    directly, matching the platform's canonical /internal/* pattern.

    Usage:
        @router.post("/internal/setup-parcel", dependencies=[Depends(verify_internal_secret)])
        async def setup_parcel(...): ...
    """
    expected = settings.internal_service_secret
    provided = x_internal_service_secret or ""
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal service secret",
        )
