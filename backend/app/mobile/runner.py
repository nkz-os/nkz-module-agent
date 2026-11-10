"""Background execution of one mobile turn.

The mobile endpoint creates the row and schedules this; the app polls the row.
Quota is checked by the endpoint before scheduling (the spend), not here.
"""

from __future__ import annotations

import dataclasses
import logging

from app.domain.session import SessionContext
from app.handlers import execute_turn
from app.mobile import turns
from app.tools.catalog import Citation

logger = logging.getLogger(__name__)


def _citation_dict(c) -> dict:
    if isinstance(c, Citation):
        return {
            "source": c.source,
            "retrieved_at": c.retrieved_at.isoformat(),
            "detail": c.detail,
        }
    if isinstance(c, dict):
        return c
    if dataclasses.is_dataclass(c):
        return dataclasses.asdict(c)
    return {"source": str(c)}


async def run_mobile_turn(
    turn_id: str, ctx: SessionContext, text: str, parcel_context: str | None
) -> None:
    """Run the turn and persist its outcome. Never raises."""
    try:
        if not await turns.mark_running(turn_id):
            # Not queued any more (reaped or already picked up): spend nothing.
            logger.warning(
                "mobile_turn_not_queued turn_id=%s tenant_id=%s trace_id=%s",
                turn_id, ctx.tenant_id, ctx.trace_id,
            )
            return
        result = await execute_turn(
            ctx, text, ctx.trace_id, context_note=parcel_context
        )
        if result.outcome in ("ok", "budget_exhausted"):
            citations = [_citation_dict(c) for c in result.citations]
            await turns.mark_done(turn_id, result.text, citations, result.outcome)
        elif result.outcome == "unconfigured":
            await turns.mark_failed(turn_id, "llm_unconfigured", result.outcome)
        else:
            await turns.mark_failed(turn_id, "error", result.outcome)
    except Exception:
        logger.exception(
            "mobile_turn_failed turn_id=%s tenant_id=%s user_id=%s trace_id=%s",
            turn_id, ctx.tenant_id, ctx.user_id, ctx.trace_id,
        )
        try:
            await turns.mark_failed(turn_id, "error", None)
        except Exception:
            logger.exception("mobile_turn_mark_failed_failed turn_id=%s", turn_id)
