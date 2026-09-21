from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from app.domain.messages import Citation, InboundMessage, OutboundMessage, VoiceRef
from app.domain.session import SessionContext


def _ctx() -> SessionContext:
    return SessionContext(
        tenant_id="tenant_a", user_id="user_a", roles=("Farmer",),
        channel="telegram", channel_user_id="42", trace_id="t-1",
    )


def test_session_context_is_immutable():
    ctx = _ctx()
    with pytest.raises(FrozenInstanceError):
        ctx.tenant_id = "tenant_b"  # type: ignore[misc]


def test_inbound_message_carries_no_tenant():
    """The channel edge knows nothing about tenants; identity resolves later."""
    fields = InboundMessage.__dataclass_fields__
    assert "tenant_id" not in fields


def test_outbound_message_defaults_to_no_citations():
    msg = OutboundMessage(text="hello")
    assert msg.citations == ()


def test_citation_records_when_the_data_was_read():
    now = datetime.now(timezone.utc)
    c = Citation(source="orion:AgriParcel", retrieved_at=now, detail="urn:x")
    assert c.retrieved_at == now


def test_voice_ref_is_a_reference_not_audio():
    """Audio bytes are never carried in domain objects."""
    fields = VoiceRef.__dataclass_fields__
    assert set(fields) == {"file_id", "duration_s", "mime_type"}
