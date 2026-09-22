"""One conversational turn.

F2 scope: linking, unlinking, and an honest placeholder reply. The agent is
wired in at F3; only the final branch changes then.
"""

from __future__ import annotations

import logging

from app.channels.telegram import start_payload
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
NOT_IMPLEMENTED = (
    "Answering questions about your parcels is not yet available. "
    "The link is active, so this chat will start working once it ships."
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

    return OutboundMessage(text=NOT_IMPLEMENTED)
