"""Which language model provider serves a given tenant.

Order: tenant row -> (platform, only when LLM_PLATFORM_FALLBACK) -> none.

A tenant WITH a row never reaches the platform branch: if its provider is no
longer allowed, or its key cannot be decrypted, the result is origin "none"
with a `notice` saying why. The platform does not pay for a tenant.

Nothing is cached: one indexed read per turn, so a DELETE, a key change or a
narrowed allowlist takes effect on the next turn in any pod. The decrypted
key lives only in the ResolvedProvider built for that one call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from app.config import get_settings
from app.llm import configs, keys
from app.llm.allowlist import parse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedProvider:
    model: str
    api_base: str
    api_key: str = field(repr=False)
    origin: Literal["tenant", "platform", "none"]
    # The provider name for LiteLLM's custom_llm_provider (tenant origin only).
    provider: str = ""
    # Why a tenant row did not resolve: None, "blocked" or "key_unreadable".
    notice: str | None = None


def _none(notice: str | None = None) -> ResolvedProvider:
    return ResolvedProvider(
        model="", api_base="", api_key="", origin="none", notice=notice
    )


async def tenant_provider(tenant_id: str | None) -> ResolvedProvider | None:
    """The tenant's own provider; None only when the tenant has NO row."""
    if tenant_id is None:
        return None
    row = await configs.get(tenant_id)
    if row is None:
        return None

    settings = get_settings()
    allowlist = parse(settings.llm_endpoint_allowlist)
    # A URL as model name can redirect some LiteLLM providers past the
    # allowlist; rows saved before the API refused it must not be used.
    if "://" in row.model:
        logger.warning("byok_model_blocked tenant_id=%s", tenant_id)
        return _none("blocked")
    if not allowlist.allows(row.provider, row.api_base):
        logger.warning(
            "byok_endpoint_blocked tenant_id=%s provider=%s", tenant_id, row.provider
        )
        return _none("blocked")
    try:
        api_key = keys.decrypt(row.api_key_ciphertext)
    except keys.KeyUnreadable:
        logger.error("byok_key_unreadable tenant_id=%s", tenant_id)
        return _none("key_unreadable")
    return ResolvedProvider(
        model=f"{row.provider}/{row.model}",
        api_base=row.api_base or "",
        api_key=api_key,
        origin="tenant",
        provider=row.provider,
    )


def platform_provider() -> ResolvedProvider | None:
    """The deployment-wide provider from settings; None if no model is set."""
    settings = get_settings()
    if not settings.llm_model:
        return None
    return ResolvedProvider(
        model=settings.llm_model,
        api_base=settings.llm_api_base,
        api_key=settings.llm_api_key,
        origin="platform",
    )


async def resolve_provider(tenant_id: str | None) -> ResolvedProvider:
    """Tenant row first (its result wins, including "none"), then platform
    only if LLM_PLATFORM_FALLBACK is on, else origin "none"."""
    tenant = await tenant_provider(tenant_id)
    if tenant is not None:
        return tenant
    if get_settings().llm_platform_fallback:
        platform = platform_provider()
        if platform is not None:
            return platform
    return _none()
