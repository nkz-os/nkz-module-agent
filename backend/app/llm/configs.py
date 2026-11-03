"""Tenant LLM config storage (BYOK).

Storage only: the endpoint allowlist is enforced by the API layer, and
decryption is the resolver's job. This module holds ciphertext and never
returns, logs or raises with a plaintext key.

Every call sets ``app.current_tenant`` inside its own transaction, as the
table has FORCE row-level security.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.db import get_pool
from app.llm import keys
from app.llm.allowlist import normalize_origin

logger = logging.getLogger(__name__)

_COLUMNS = (
    "tenant_id, provider, model, api_base, api_key_ciphertext, api_key_last4, "
    "max_tokens_per_turn, updated_by, updated_at"
)


class KeyRequired(Exception):
    """A key is needed: first save, or provider/endpoint changed."""


class BYOKUnavailable(Exception):
    """No usable master key: nothing may be stored."""


@dataclass(frozen=True)
class TenantLLMConfig:
    tenant_id: str
    provider: str
    model: str
    api_base: str | None
    api_key_ciphertext: bytes
    api_key_last4: str
    max_tokens_per_turn: int | None
    updated_by: str
    updated_at: datetime


def _from_row(row) -> TenantLLMConfig:
    return TenantLLMConfig(
        tenant_id=row["tenant_id"],
        provider=row["provider"],
        model=row["model"],
        api_base=row["api_base"],
        api_key_ciphertext=bytes(row["api_key_ciphertext"]),
        api_key_last4=row["api_key_last4"],
        max_tokens_per_turn=row["max_tokens_per_turn"],
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
    )


def _clean_base(api_base: str | None) -> str | None:
    if api_base is None:
        return None
    value = api_base.strip()
    return value or None


def _origin_key(api_base: str | None) -> str | None:
    """Comparable identity of an endpoint: its normalized origin.

    An unparseable value gets a distinct marker so it never equals a valid one.
    """
    if api_base is None:
        return None
    return normalize_origin(api_base) or f"invalid:{api_base}"


async def get(tenant_id: str) -> TenantLLMConfig | None:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant', $1, true)", tenant_id)
        row = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM tenant_llm_configs WHERE tenant_id = $1", tenant_id
        )
    return _from_row(row) if row else None


async def upsert(
    tenant_id: str,
    *,
    provider: str,
    model: str,
    api_base: str | None,
    api_key: str | None,
    max_tokens_per_turn: int | None,
    updated_by: str,
) -> TenantLLMConfig:
    """Save the tenant's config. ``api_key`` None/blank keeps the stored key,
    but only if provider and endpoint origin are unchanged."""
    if not keys.available():
        raise BYOKUnavailable("LLM key encryption is not configured")
    base = _clean_base(api_base)
    new_key = api_key if api_key and api_key.strip() else None

    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant', $1, true)", tenant_id)
        existing = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM tenant_llm_configs WHERE tenant_id = $1 FOR UPDATE",
            tenant_id,
        )
        if new_key is not None:
            ciphertext = keys.encrypt(new_key)
            last4 = keys.last4(new_key)
        else:
            if existing is None:
                raise KeyRequired("an API key is required")
            if (
                existing["provider"].casefold() != provider.casefold()
                or _origin_key(existing["api_base"]) != _origin_key(base)
            ):
                raise KeyRequired("provider or endpoint changed: re-enter the API key")
            ciphertext = bytes(existing["api_key_ciphertext"])
            last4 = existing["api_key_last4"]

        row = await conn.fetchrow(
            f"""
            INSERT INTO tenant_llm_configs
                (tenant_id, provider, model, api_base, api_key_ciphertext,
                 api_key_last4, max_tokens_per_turn, updated_by, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
            ON CONFLICT (tenant_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                api_base = EXCLUDED.api_base,
                api_key_ciphertext = EXCLUDED.api_key_ciphertext,
                api_key_last4 = EXCLUDED.api_key_last4,
                max_tokens_per_turn = EXCLUDED.max_tokens_per_turn,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            RETURNING {_COLUMNS}
            """,
            tenant_id, provider, model, base, ciphertext, last4,
            max_tokens_per_turn, updated_by,
        )
    logger.info("tenant_llm_config_saved", extra={"tenant_id": tenant_id, "provider": provider})
    return _from_row(row)


async def delete(tenant_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant', $1, true)", tenant_id)
        status = await conn.execute(
            "DELETE FROM tenant_llm_configs WHERE tenant_id = $1", tenant_id
        )
    return status.endswith(" 1")
