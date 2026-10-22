"""One conversational turn.

F2 scope: linking, unlinking, and an honest placeholder reply. F3 wires the
final branch to the agent: voice/empty gate first (a voice note or a message
with no text is refused and audited before quota and before the model, so it
costs and counts nothing), then quota check (a blocked turn short-circuits
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
from app.agent.loop import TurnResult, run_turn
from app.agent.quota import check_and_count
from app.audit.repository import record_turn
from app.channels.telegram import start_payload
from app.config import get_settings
from app.domain.messages import InboundMessage, OutboundMessage
from app.identity import repository as repo
from app.identity import service

logger = logging.getLogger(__name__)

# Backend strings are neutral English, except the two bilingual constants
# below: they are the only reply a farmer sees when they did not type text,
# and the backend has no i18n layer to pick a language from.
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
VOICE_NOT_READY_TEXT = (
    "Las notas de voz aún no están disponibles. Escríbeme tu consulta como texto. "
    "/ Voice notes are not available yet. Please type your question."
)
EMPTY_MESSAGE_TEXT = (
    "Por ahora solo puedo leer mensajes de texto. "
    "/ For now I can only read text messages."
)


async def _refuse(
    session, marker: str, reply: str, started: float, trace_id: str,
    msg: InboundMessage,
) -> OutboundMessage:
    """Audit a gated turn and return the fixed reply.

    The audit row stores a marker, never the user's input or the voice file id.
    """
    result = TurnResult(
        text=marker, outcome="refused", model=None, tool_calls=(),
        tokens_prompt=0, tokens_completion=0,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    await record_turn(session, None, result, latency_ms, trace_id)
    extra = f" duration_s={msg.voice.duration_s}" if msg.voice is not None else ""
    logger.info(
        "turn_refused reason=%s tenant_id=%s user_id=%s channel=%s trace_id=%s%s",
        marker, session.tenant_id, session.user_id, msg.channel, trace_id, extra,
    )
    return OutboundMessage(text=reply)


async def handle_message(msg: InboundMessage, trace_id: str) -> OutboundMessage:
    has_text = bool(msg.text and msg.text.strip())
    token = start_payload(msg) if has_text else None
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

    if not has_text:
        # Hard gate before /unlink, quota and model: nothing readable was
        # sent, so there is nothing to spend on and nothing to count.
        gate_started = time.monotonic()
        if msg.voice is not None:
            return await _refuse(
                session, "voice_unsupported", VOICE_NOT_READY_TEXT,
                gate_started, trace_id, msg,
            )
        return await _refuse(
            session, "empty_message", EMPTY_MESSAGE_TEXT, gate_started, trace_id, msg,
        )

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
    result = await run_turn(msg.text or "", budget, session)
    latency_ms = int((time.monotonic() - started) * 1000)

    await record_turn(session, msg.text, result, latency_ms, trace_id)

    return OutboundMessage(text=result.text)
