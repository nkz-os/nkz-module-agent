"""Repository over agent_mobile_turns (migration 005).

The table has no row-level security: isolation is the full key. Every read
qualifies tenant_id AND user_id; the only statements keyed by turn_id alone
(mark_*) act on a row the caller itself just created or fetched under that key.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.config import get_settings
from app.db import get_pool

logger = logging.getLogger(__name__)

# One tool timeout of slack past the turn deadline (a tool may finish late).
_TOOL_SLACK_SECONDS = 15


def orphan_max_age(settings=None) -> int:
    """Age after which a queued/running turn is presumed dead."""
    s = settings or get_settings()
    return (
        s.turn_timeout_seconds + _TOOL_SLACK_SECONDS
        + s.mobile_turn_orphan_grace_seconds
    )

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


async def get_by_client_turn_id(
    tenant_id: str, user_id: str, client_turn_id: str
) -> MobileTurn | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_COLUMNS} FROM agent_mobile_turns
             WHERE tenant_id = $1 AND user_id = $2 AND client_turn_id = $3
            """,
            tenant_id, user_id, client_turn_id,
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
    """Newest first, within the memory TTL; stale open rows are failed first."""
    settings = get_settings()
    await expire_orphans_for_user(tenant_id, user_id, orphan_max_age(settings))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM agent_mobile_turns
             WHERE tenant_id = $1 AND user_id = $2
               AND created_at > now() - make_interval(hours => $4)
             ORDER BY created_at DESC, turn_id DESC
             LIMIT $3
            """,
            tenant_id, user_id, limit, int(settings.memory_ttl_hours),
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


async def _log_skipped(conn, turn_id: str) -> None:
    """A terminal write that touched no row must never vanish silently."""
    state = await conn.fetchval(
        "SELECT state FROM agent_mobile_turns WHERE turn_id = $1",
        _as_uuid(turn_id),
    )
    logger.warning(
        "mobile_turn_terminal_overwrite_skipped turn_id=%s state=%s",
        turn_id, state,
    )


async def mark_done(
    turn_id: str, reply_text: str, citations: list[dict], outcome: str
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE agent_mobile_turns
               SET state = 'done', reply_text = $2, citations = $3::jsonb,
                   outcome = $4, finished_at = now()
             WHERE turn_id = $1 AND state IN ('queued', 'running')
            RETURNING turn_id
            """,
            _as_uuid(turn_id), reply_text, json.dumps(citations), outcome,
        )
        if row is None:
            await _log_skipped(conn, turn_id)


async def mark_failed(turn_id: str, error_code: str, outcome: str | None) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE agent_mobile_turns
               SET state = 'failed', error_code = $2, outcome = $3,
                   finished_at = now()
             WHERE turn_id = $1 AND state IN ('queued', 'running')
            RETURNING turn_id
            """,
            _as_uuid(turn_id), error_code, outcome,
        )
        if row is None:
            await _log_skipped(conn, turn_id)


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


async def expire_orphans_for_user(
    tenant_id: str, user_id: str, max_age_seconds: int
) -> int:
    """expire_orphans scoped to one user (read-time reaper)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE agent_mobile_turns
               SET state = 'failed', error_code = 'interrupted',
                   finished_at = now()
             WHERE tenant_id = $1 AND user_id = $2
               AND state IN ('queued', 'running')
               AND created_at <= now() - make_interval(secs => $3)
            RETURNING turn_id
            """,
            tenant_id, user_id, float(max_age_seconds),
        )
    return len(rows)
