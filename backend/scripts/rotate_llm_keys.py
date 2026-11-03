"""Re-encrypt every tenant LLM key under the first master key.

Run after prepending a new key to LLM_KEY_ENCRYPTION_KEYS ("new,old"). Safe to
re-run: rows the first key already reads are left untouched. Prints counts
only, never key material.

The table has FORCE row-level security, so enumerating tenants needs a DSN
whose role bypasses RLS (AGENT_ROTATE_DSN, falling back to AGENT_MIGRATE_DSN);
each row is then updated inside a transaction with app.current_tenant set to
that row's tenant, like the service does.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm import keys  # noqa: E402


async def rotate(dsn: str) -> tuple[int, int]:
    """Return (rotated, unreadable)."""
    if not keys.available():
        raise SystemExit("LLM_KEY_ENCRYPTION_KEYS has no usable key")
    rotated = unreadable = 0
    conn = await asyncpg.connect(
        dsn, server_settings={"search_path": "agent_module,public"}
    )
    try:
        tenants = [
            r["tenant_id"]
            for r in await conn.fetch("SELECT tenant_id FROM tenant_llm_configs")
        ]
        for tenant in tenants:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_tenant', $1, true)", tenant
                )
                token = await conn.fetchval(
                    "SELECT api_key_ciphertext FROM tenant_llm_configs "
                    "WHERE tenant_id = $1 FOR UPDATE",
                    tenant,
                )
                if token is None or keys.uses_primary_key(token):
                    continue
                try:
                    new = keys.reencrypt(token)
                except keys.KeyUnreadable:
                    unreadable += 1
                    continue
                await conn.execute(
                    "UPDATE tenant_llm_configs SET api_key_ciphertext = $1 "
                    "WHERE tenant_id = $2",
                    new, tenant,
                )
                rotated += 1
    finally:
        await conn.close()
    return rotated, unreadable


if __name__ == "__main__":
    dsn = os.environ.get("AGENT_ROTATE_DSN") or os.environ.get("AGENT_MIGRATE_DSN")
    if not dsn:
        raise SystemExit("AGENT_ROTATE_DSN (or AGENT_MIGRATE_DSN) is not set")
    done, bad = asyncio.run(rotate(dsn))
    print(f"[rotate] rotated={done} unreadable={bad}", flush=True)
