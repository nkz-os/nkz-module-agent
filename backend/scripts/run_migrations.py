"""Apply the module's own SQL migrations in order (see backend/migrations/).

Idempotent: re-running is safe. The migrations assume the platform's
DB-roles contract: the ``nkz_module_agent`` login role already exists (the
platform creates it and seals its password; this repo never creates roles).
The module owns the ``agent_module`` schema, so after that schema exists any
further migration runs with the service's own least-privilege POSTGRES_URL.

AGENT_MIGRATE_DSN is only needed to bootstrap that schema on a database that
does not have it yet (CREATE SCHEMA ... AUTHORIZATION requires CREATE on the
database). On the first run point it at a role with that one privilege; set it
to the module's own URL for every run after the schema exists.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


async def _apply_migrations() -> None:
    dsn = os.environ.get("AGENT_MIGRATE_DSN")
    if not dsn:
        raise SystemExit(
            "AGENT_MIGRATE_DSN is not set: migrations need a DSN able to "
            "CREATE SCHEMA for the first bootstrap; the module's own "
            "POSTGRES_URL works once the schema exists. The platform role "
            "nkz_module_agent must already exist (platform DB-roles migration)."
        )
    conn = await asyncpg.connect(dsn)
    try:
        for m in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
            print(f"[migrate] applying {m.name}", flush=True)
            await conn.execute(m.read_text())
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_apply_migrations())
    print("[migrate] done", flush=True)
