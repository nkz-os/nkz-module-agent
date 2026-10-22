"""One row per conversational turn.

Written after the turn resolves, success or failure — the failed turn is the one
someone asks about later, so recording only successes answers a question nobody
has.

Retention: the append-only convention of migration 002 has one deliberate,
owner-approved amendment -- after ``audit_text_retention_days`` the free text
(inbound_text, reply_text) is set to NULL while the metadata row stays. Rows
are never deleted.

Never raises into the turn. Losing an audit row is bad; losing the farmer's
answer because the audit write failed is worse.
"""

from __future__ import annotations

import json
import logging
import time

from app.agent.loop import TurnResult
from app.config import get_settings
from app.db import get_pool
from app.domain.session import SessionContext

logger = logging.getLogger(__name__)

# Scrub cadence: the UPDATE is a tenant-scoped range scan on
# (tenant_id, created_at). It runs on the FIRST recorded turn of a tenant in
# this process and then at most once per _SCRUB_INTERVAL_SECONDS for that
# tenant, so the hot path pays one indexed UPDATE per tenant per pod per day.
# A process-global "every Nth turn" counter would scrub whichever tenant held
# the Nth turn, starving quiet tenants.
_SCRUB_INTERVAL_SECONDS = 24 * 60 * 60

# {tenant_id: monotonic timestamp of its last successful scrub in this process}.
_last_scrub: dict[str, float] = {}


def _monotonic() -> float:
    return time.monotonic()


def _should_scrub(tenant_id: str) -> bool:
    last = _last_scrub.get(tenant_id)
    return last is None or _monotonic() - last >= _SCRUB_INTERVAL_SECONDS


async def scrub_expired_texts(tenant_id: str) -> int:
    """NULL the free text of this tenant's audit rows older than the window.

    Per tenant because the table has FORCE row-level security: the module's
    role only sees the rows of the tenant set in ``app.current_tenant``, so a
    global sweep is impossible. What is guaranteed: one indexed UPDATE per
    tenant per day per pod, fired by that tenant's own turns. A tenant that
    stops talking keeps its old texts until its next turn (and a pod restart
    only brings the next scrub forward, never back).

    Runs in its own transaction so a failure here can never roll back the
    audit INSERT that triggered it. The quota is unaffected: it counts rows by
    ``created_at``, which this never changes. Returns the rows touched.
    """
    settings = get_settings()
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant', $1, true)", tenant_id
        )
        status = await conn.execute(
            """
            UPDATE agent_turn_audit
               SET inbound_text = NULL, reply_text = NULL
             WHERE tenant_id = $1
               AND created_at < now() - ($2 || ' days')::interval
               AND (inbound_text IS NOT NULL OR reply_text IS NOT NULL)
            """,
            tenant_id, str(settings.audit_text_retention_days),
        )
    return int(status.rsplit(" ", 1)[-1])


async def record_turn(
    ctx: SessionContext,
    inbound_text: str | None,
    result: TurnResult,
    latency_ms: int,
    trace_id: str,
) -> None:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # The audit table is RLS tenant-scoped: the policy reads the
                # tenant from this session setting. is_local=true gives SET
                # LOCAL semantics, so the context dies with the transaction and
                # a recycled pool connection never leaks one tenant into the
                # next. Inert while the pool connects as superuser, load-bearing
                # once the service connects with its least-privilege role.
                await conn.execute(
                    "SELECT set_config('app.current_tenant', $1, true)",
                    ctx.tenant_id,
                )
                await conn.execute(
                    """
                    INSERT INTO agent_turn_audit
                           (trace_id, tenant_id, user_id, channel, channel_user_id,
                            inbound_text, model, tool_calls, reply_text,
                            tokens_prompt, tokens_completion, latency_ms, outcome)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13)
                    """,
                    trace_id, ctx.tenant_id, ctx.user_id, ctx.channel,
                    ctx.channel_user_id, inbound_text, result.model,
                    json.dumps(list(result.tool_calls)), result.text,
                    result.tokens_prompt, result.tokens_completion, latency_ms,
                    result.outcome,
                )
    except Exception:
        logger.exception(
            "audit_write_failed trace_id=%s tenant_id=%s outcome=%s",
            trace_id, ctx.tenant_id, result.outcome,
        )

    if _should_scrub(ctx.tenant_id):
        try:
            rows = await scrub_expired_texts(ctx.tenant_id)
            _last_scrub[ctx.tenant_id] = _monotonic()
            logger.info(
                "audit_texts_scrubbed tenant_id=%s rows=%s", ctx.tenant_id, rows
            )
        except Exception:
            logger.exception("audit_scrub_failed tenant_id=%s", ctx.tenant_id)
