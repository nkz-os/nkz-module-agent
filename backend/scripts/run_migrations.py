"""Apply the module's own SQL migrations in order (see backend/migrations/).

Idempotent: re-running is safe. The dedicated ``agent_module`` schema and the
``agent_module`` least-privilege role are created here; the role's password is
assigned out-of-band, never in a public repo.
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
            "AGENT_MIGRATE_DSN is not set: migrations need a privileged DSN "
            "(CREATE ROLE / CREATE SCHEMA / ALTER TABLE ... SET SCHEMA / GRANT) "
            "— the service's own POSTGRES_URL is NOT enough."
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
