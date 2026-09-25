"""One conversational turn.

F2 scope: linking, unlinking, and an honest placeholder reply. F3 wires the
final branch to the agent: quota check first (a blocked turn short-circuits
before the model -- the cap exists to stop spend), then a settings-derived
TurnBudget around run_turn, then an append-only audit row.

Quota race (documented, accepted for v1): check_and_count and record_turn are
two separate statements, so two concurrent webhooks can both pass the check
before either one audits. A burst slightly above the cap can slip through.
Conservative caps contain the blast radius; a serialized check-and-audit path
is a later task if needed.
"""

from __future__ import annotations

import logging
import time

from app.agent.budget import TurnBudget
from app.agent.loop import run_turn
from app.agent.quota import check_and_count
from app.audit.repository import record_turn
from app.channels.telegram import start_payload
from app.config import get_settings
from app.domain.messages import InboundMessage, OutboundMessage
from app.identity import repository as repo
from app.identity import service

logger = logging.getLogger(__name__)

# Backend strings are neutral English; user-facing localisation happens in F3
# when replies stop being fixed text.
NOT_LINKED = (
    "This account is not linked yet. Open the platform, go to the Agent panel "
    "and use the link button to connect this chat."
)
LINK_FAILED = "That link is invalid or has expired. Generate a new one from the platform."
LINK_OK = (
    "Linked. You are talking to an automated assistant. "
    "Send /unlink at any time to disconnect this chat."
)
UNLINK_OK = "Unlinked. This chat is no longer connected to your account."
QUOTA_TEXT = (
    "This account has reached its usage limit for now. Please try again later."
)


async def handle_message(msg: InboundMessage, trace_id: str) -> OutboundMessage:
    token = start_payload(msg)
    if token is not None:
        ctx = await service.redeem_link_token(token, msg.channel, msg.channel_user_id)
        if ctx is None:
            logger.warning(
                "link_redeem_failed channel=%s trace_id=%s", msg.channel, trace_id
            )
            return OutboundMessage(text=LINK_FAILED)
        logger.info(
            "link_created tenant_id=%s user_id=%s channel=%s trace_id=%s",
            ctx.tenant_id, ctx.user_id, ctx.channel, trace_id,
        )
        return OutboundMessage(text=LINK_OK)

    session = await service.resolve_session(msg.channel, msg.channel_user_id, trace_id)
    if session is None:
        return OutboundMessage(text=NOT_LINKED)

    if msg.text and msg.text.strip() == "/unlink":
        link = await repo.get_active_link(msg.channel, msg.channel_user_id)
        if link is not None:
            await repo.revoke_link(link["id"], session.tenant_id)
        logger.info(
            "link_revoked tenant_id=%s user_id=%s trace_id=%s",
            session.tenant_id, session.user_id, trace_id,
        )
        return OutboundMessage(text=UNLINK_OK)

    blocked = await check_and_count(
        session.tenant_id, msg.channel, msg.channel_user_id
    )
    if blocked is not None:
        # Short-circuit before the model: the cap exists to stop spend, so
        # calling it and then refusing would spend exactly what it saves.
        return OutboundMessage(text=QUOTA_TEXT)

    settings = get_settings()
    budget = TurnBudget(
        max_iterations=settings.max_iterations,
        max_tool_calls=settings.max_tool_calls,
        max_tokens=settings.max_tokens_per_turn,
        timeout_s=settings.turn_timeout_seconds,
    )
    started = time.monotonic()
    result = await run_turn(msg.text or "", budget)
    latency_ms = int((time.monotonic() - started) * 1000)

    await record_turn(session, msg.text, result, latency_ms, trace_id)

    return OutboundMessage(text=result.text)
