"""Telegram adapter."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.messages import InboundMessage, OutboundMessage, VoiceRef

CHANNEL = "telegram"


class TelegramAdapter:
    name = CHANNEL

    def parse(self, raw: dict) -> InboundMessage | None:
        message = raw.get("message")
        if not message:
            return None

        sender = message.get("from") or {}
        sender_id = sender.get("id")
        if sender_id is None:
            return None

        voice_raw = message.get("voice")
        voice = (
            VoiceRef(
                file_id=voice_raw["file_id"],
                duration_s=int(voice_raw.get("duration", 0)),
                mime_type=voice_raw.get("mime_type", "audio/ogg"),
            )
            if voice_raw
            else None
        )

        return InboundMessage(
            channel=CHANNEL,
            channel_user_id=str(sender_id),
            text=message.get("text"),
            voice=voice,
            idempotency_key=f"{CHANNEL}:{raw['update_id']}",
            received_at=datetime.fromtimestamp(
                message.get("date", 0), tz=timezone.utc
            ),
        )

    def render(self, msg: OutboundMessage, chat_id: str) -> dict:
        text = msg.text
        if msg.citations:
            lines = "\n".join(
                f"· {c.source} — {c.retrieved_at:%Y-%m-%d}"
                + (f" ({c.detail})" if c.detail else "")
                for c in msg.citations
            )
            text = f"{text}\n\n{lines}"
        return {"chat_id": chat_id, "text": text}


def start_payload(msg: InboundMessage) -> str | None:
    """Return the token from a `/start <token>` message, if there is one."""
    if not msg.text:
        return None
    parts = msg.text.split(maxsplit=1)
    if parts[0] != "/start" or len(parts) < 2:
        return None
    return parts[1].strip() or None
