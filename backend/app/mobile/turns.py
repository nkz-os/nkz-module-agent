"""Repository over agent_mobile_turns (migration 005).

The table has no row-level security: isolation is the full key. Every read
qualifies tenant_id AND user_id; the only statements keyed by turn_id alone
(mark_*) act on a row the caller itself just created or fetched under that key.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.db import get_pool

_COLUMNS = (
    "turn_id, tenant_id, user_id, client_turn_id, parcel_id, lang, text, state, "
    "reply_text, citations, outcome, error_code, trace_id, created_at, "
    "started_at, finished_at"
)


@dataclass(frozen=True)
class MobileTurn:
    turn_id: str
    tenant_id: str
    user_id: str
    client_turn_id: str
    parcel_id: str | None
    lang: str
    text: str
    state: str
    reply_text: str | None
    citations: list[dict]
    outcome: str | None
    error_code: str | None
    trace_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _row(r) -> MobileTurn:
    citations = r["citations"]
    if isinstance(citations, str):
        citations = json.loads(citations)
    return MobileTurn(
        turn_id=str(r["turn_id"]),
        tenant_id=r["tenant_id"],
        user_id=r["user_id"],
        client_turn_id=r["client_turn_id"],
        parcel_id=r["parcel_id"],
        lang=r["lang"],
        text=r["text"],
        state=r["state"],
        reply_text=r["reply_text"],
        citations=list(citations or []),
        outcome=r["outcome"],
        error_code=r["error_code"],
        trace_id=r["trace_id"],
        created_at=r["created_at"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
    )


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


async def create_turn(
    tenant_id: str,
    user_id: str,
    client_turn_id: str,
    text: str,
    parcel_id: str | None,
    lang: str,
    trace_id: str,
) -> tuple[MobileTurn, bool]:
    """Insert a queued turn; a retried submit returns the existing row."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO agent_mobile_turns
                (tenant_id, user_id, client_turn_id, text, parcel_id, lang,
                 trace_id, state)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'queued')
            ON CONFLICT (tenant_id, user_id, client_turn_id) DO NOTHING
            RETURNING {_COLUMNS}
            """,
            tenant_id, user_id, client_turn_id, text, parcel_id, lang, trace_id,
        )
        if row is not None:
            return _row(row), True
        row = await conn.fetchrow(
            f"""
            SELECT {_COLUMNS} FROM agent_mobile_turns
             WHERE tenant_id = $1 AND user_id = $2 AND client_turn_id = $3
            """,
            tenant_id, user_id, client_turn_id,
        )
    return _row(row), False


async def get_turn(tenant_id: str, user_id: str, turn_id: str) -> MobileTurn | None:
    tid = _as_uuid(turn_id)
    if tid is None:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_COLUMNS} FROM agent_mobile_turns
             WHERE tenant_id = $1 AND user_id = $2 AND turn_id = $3
            """,
            tenant_id, user_id, tid,
        )
    return _row(row) if row else None


async def has_open_turn(tenant_id: str, user_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM agent_mobile_turns
                     WHERE tenant_id = $1 AND user_id = $2
                       AND state IN ('queued', 'running'))
                """,
                tenant_id, user_id,
            )
        )


async def list_history(tenant_id: str, user_id: str, limit: int) -> list[MobileTurn]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM agent_mobile_turns
             WHERE tenant_id = $1 AND user_id = $2
             ORDER BY created_at DESC, turn_id DESC
             LIMIT $3
            """,
            tenant_id, user_id, limit,
        )
    return [_row(r) for r in rows]


async def mark_running(turn_id: str) -> bool:
    """True when this call moved the row queued -> running."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE agent_mobile_turns SET state = 'running', started_at = now()
             WHERE turn_id = $1 AND state = 'queued'
            RETURNING turn_id
            """,
            _as_uuid(turn_id),
        )
    return row is not None


async def mark_done(
    turn_id: str, reply_text: str, citations: list[dict], outcome: str
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_mobile_turns
               SET state = 'done', reply_text = $2, citations = $3::jsonb,
                   outcome = $4, finished_at = now()
             WHERE turn_id = $1 AND state IN ('queued', 'running')
            """,
            _as_uuid(turn_id), reply_text, json.dumps(citations), outcome,
        )


async def mark_failed(turn_id: str, error_code: str, outcome: str | None) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_mobile_turns
               SET state = 'failed', error_code = $2, outcome = $3,
                   finished_at = now()
             WHERE turn_id = $1 AND state IN ('queued', 'running')
            """,
            _as_uuid(turn_id), error_code, outcome,
        )


async def expire_orphans(max_age_seconds: int) -> int:
    """Fail queued/running turns older than the age (their task died with a pod)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE agent_mobile_turns
               SET state = 'failed', error_code = 'interrupted',
                   finished_at = now()
             WHERE state IN ('queued', 'running')
               AND created_at <= now() - make_interval(secs => $1)
            RETURNING turn_id
            """,
            float(max_age_seconds),
        )
    return len(rows)
