"""Channel-neutral message objects.

The core never sees a Telegram update and never builds a Telegram payload;
adapters translate at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class VoiceRef:
    """A reference to audio held by the channel — never the audio itself."""

    file_id: str
    duration_s: int
    mime_type: str


@dataclass(frozen=True)
class InboundMessage:
    """channel_user_id is the SENDER — the single source of identity, used to
    resolve/link a tenant session. conversation_id is where the reply must be
    delivered. They are the same value in a private chat but differ in a
    group: identity must still follow the sender there, delivery must not —
    replying to the sender instead of the conversation answers in the wrong
    place, or fails outright if the bot cannot open a private chat with them.
    """

    channel: str
    channel_user_id: str
    conversation_id: str
    text: str | None
    voice: VoiceRef | None
    idempotency_key: str
    received_at: datetime


@dataclass(frozen=True)
class Citation:
    """Where a claim came from, and when it was read."""

    source: str
    retrieved_at: datetime
    detail: str | None = None


@dataclass(frozen=True)
class OutboundMessage:
    text: str
    citations: tuple[Citation, ...] = ()
