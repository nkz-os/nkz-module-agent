"""Which language model provider serves a given tenant.

Order: tenant -> platform -> none (unconfigured). In F3b only the platform
branch exists (the deployment's own LLM_* settings), but this function, its
order and its tests are already the F3c contract: adding the tenant branch
means touching this one file, not the facade or the loop.

F3c rule, frozen here (design F3b section 5): the tenant's key is stored
encrypted in `agent_module` (encryption key in its own SealedSecret), endpoints
are restricted by a per-deployment allowlist, platform quotas apply with NO
relaxation, and `origin` is audited.

No I/O and no litellm import: this runs on every turn and must stay cheap
and free of control-flow exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import get_settings


@dataclass(frozen=True)
class ResolvedProvider:
    model: str
    api_base: str
    api_key: str
    origin: Literal["tenant", "platform", "none"]


def tenant_provider(tenant_id: str | None) -> ResolvedProvider | None:
    """The tenant's own provider (F3c). Always None in F3b.

    The F3c contract (design F3b section 5): key encrypted in `agent_module`,
    per-deployment endpoint allowlist, platform quotas without relaxation,
    `origin` audited. `tenant_llm_configs` does not exist yet; this is the
    only place F3c has to fill in.
    """
    return None


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


def resolve_provider(tenant_id: str | None) -> ResolvedProvider:
    """Tenant provider first, then platform, else an origin="none" provider.

    Without a tenant id the tenant branch is not consulted.
    """
    if tenant_id is not None:
        tenant = tenant_provider(tenant_id)
        if tenant is not None:
            return tenant
    platform = platform_provider()
    if platform is not None:
        return platform
    return ResolvedProvider(model="", api_base="", api_key="", origin="none")
