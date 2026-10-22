"""The channel boundary.

Everything above this line speaks InboundMessage/OutboundMessage. Adding a
channel means adding an adapter, not touching the core.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.messages import InboundMessage, OutboundMessage


class ChannelAdapter(Protocol):
    name: str

    def parse(self, raw: dict) -> InboundMessage | None:
        """Translate a raw platform update. None means: nothing to handle."""
        ...

    def render(self, msg: OutboundMessage, chat_id: str) -> dict:
        """Translate a reply into this platform's send payload."""
        ...
