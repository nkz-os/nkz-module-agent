"""One row per conversational turn.

Written after the turn resolves, success or failure — the failed turn is the one
someone asks about later, so recording only successes answers a question nobody
has.

Never raises into the turn. Losing an audit row is bad; losing the farmer's
answer because the audit write failed is worse.
"""

from __future__ import annotations

import json
import logging

from app.agent.loop import TurnResult
from app.db import get_pool
from app.domain.session import SessionContext

logger = logging.getLogger(__name__)


async def record_turn(
    ctx: SessionContext,
    inbound_text: str | None,
    result: TurnResult,
    latency_ms: int,
    trace_id: str,
) -> None:
    try:
        pool = await get_pool()
        await pool.execute(
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
