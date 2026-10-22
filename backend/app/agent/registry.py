"""The set of tools offered to the model, intersected with tenant installs.

Tool adapters are written here, in this repository, by the team that owns the
agent -- not declared by each module. That was decided so the AI surface stays
in one replaceable place instead of being spread across every module's repo,
and it is why nothing external can put text in front of the model.

The catalogue (``app.tools.catalog``) carries every adapter with its
``required_module`` marker (None = core, always available) and, for tools
backed by a service URL, a ``required_setting``: an empty URL means the tool is
not offered at all. ``tools_for``
intersects that catalogue with the tenant's enabled rows in the platform view
``public.module_installations``. The registry is fail-open: if that shared
view is missing or the database is down, the turn falls back to the core
subset rather than losing the conversation -- and logs one warning per
failed attempt (failures are not cached, so the next turn retries).
"""

from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.db import get_pool
from app.tools.catalog import ToolDefinition, available_tools

logger = logging.getLogger(__name__)

# {tenant_id: (monotonic_timestamp, cached_tools)}. Keyed by tenant: one
# tenant's installs must never leak into another tenant's cache entry.
_cache: dict[str, tuple[float, tuple[ToolDefinition, ...]]] = {}


async def tools_for(tenant_id: str) -> tuple[ToolDefinition, ...]:
    """The catalogue intersected with the tenant's enabled installs.

    Cache is in-memory, keyed by tenant_id, TTL ``REGISTRY_CACHE_SECONDS``.
    A failed read of ``public.module_installations`` (view missing, database
    down) never raises: it returns the core subset and logs one warning.
    """
    now = time.monotonic()
    cached = _cache.get(tenant_id)
    if cached is not None and now - cached[0] < get_settings().registry_cache_seconds:
        return cached[1]

    settings = get_settings()
    # A tool whose service URL is not configured can only ever answer
    # "unreachable"; do not offer it. Applies to the fail-open subset too.
    catalogue = tuple(
        t
        for t in available_tools()
        if t.required_setting is None or getattr(settings, t.required_setting, "")
    )
    try:
        installed = await _installed_module_ids(tenant_id)
    except Exception:
        # One warning per failed cycle, never one per tool: the caller gets
        # the core tools and the turn carries on. exc_info stays for the
        # operator's log; it never reaches the model.
        logger.warning(
            "registry_installations_query_failed tenant_id=%s fallback=core",
            tenant_id,
            exc_info=True,
        )
        return tuple(t for t in catalogue if t.required_module is None)

    tools = tuple(
        t
        for t in catalogue
        if t.required_module is None or t.required_module in installed
    )
    _cache[tenant_id] = (now, tools)
    return tools


async def _installed_module_ids(tenant_id: str) -> set[str]:
    """Enabled module_ids for one tenant, straight from the platform view.

    Qualified ``public.``: the pool's search_path is ``agent_module,public``,
    and the module owns no table of this name, so the platform's shared view
    must be named explicitly rather than resolved by search_path.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT module_id FROM public.module_installations "
            "WHERE tenant_id = $1 AND is_enabled",
            tenant_id,
        )
    return {row["module_id"] for row in rows}


def clear_cache() -> None:
    """Drop every cached tenant intersection. Exists for tests."""
    _cache.clear()


def openai_spec(tool: ToolDefinition) -> dict:
    """The OpenAI-style tool schema handed to the provider."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.args_model.model_json_schema(),
        },
    }


async def tool_specs(tenant_id: str) -> list[dict]:
    """OpenAI-style schemas for the tools available to ``tenant_id``."""
    return [openai_spec(tool) for tool in await tools_for(tenant_id)]


async def has_tools(tenant_id: str) -> bool:
    """Whether ``tenant_id`` has at least one tool available."""
    return bool(await tools_for(tenant_id))
