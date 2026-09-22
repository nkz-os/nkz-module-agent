from datetime import datetime, timezone

import pytest

from app import handlers
from app.domain.messages import InboundMessage
from app.handlers import handle_message
from app.identity import repository as repo
from app.identity import service
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


def _msg(text: str, user: str = "42") -> InboundMessage:
    return InboundMessage(
        channel="telegram", channel_user_id=user, conversation_id=user, text=text,
        voice=None, idempotency_key=f"telegram:{user}",
        received_at=datetime.now(timezone.utc),
    )


async def test_unlinked_user_is_told_to_link(db_pool):
    reply = await handle_message(_msg("hola", user="999"), trace_id="t-1")
    assert reply.text == handlers.NOT_LINKED


async def test_start_with_valid_token_links_the_account(db_pool):
    token, _ = await service.create_link_token("tenant_a", "user_a", ("Farmer",))

    reply = await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    assert reply.text == handlers.LINK_OK
    ctx = await service.resolve_session("telegram", "42", "t-2")
    assert ctx is not None and ctx.tenant_id == "tenant_a"


async def test_link_confirmation_discloses_it_is_automated(db_pool):
    """Platform requirement: the user must know they are talking to software."""
    token, _ = await service.create_link_token("tenant_a", "user_a", ())

    reply = await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    assert "automated" in reply.text.lower()
    assert reply.text == handlers.LINK_OK


async def test_start_with_bad_token_does_not_link(db_pool):
    reply = await handle_message(_msg("/start not-a-real-token"), trace_id="t-1")
    assert reply.text == handlers.LINK_FAILED
    assert await service.resolve_session("telegram", "42", "t-2") is None


async def test_unlink_revokes_the_link(db_pool):
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    reply = await handle_message(_msg("/unlink"), trace_id="t-2")

    assert reply.text == handlers.UNLINK_OK
    assert await service.resolve_session("telegram", "42", "t-3") is None


async def test_linked_user_gets_the_read_only_notice(db_pool):
    """F2 has no agent yet; the reply must not pretend otherwise."""
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    await handle_message(_msg(f"/start {token}"), trace_id="t-1")

    reply = await handle_message(_msg("¿cómo va mi parcela?"), trace_id="t-2")

    assert reply.text == handlers.NOT_IMPLEMENTED


async def test_unlink_cannot_revoke_another_tenants_link(db_pool):
    """Security property: one tenant must never revoke another tenant's link.

    handle_message can never itself send a cross-tenant revoke — the tenant it
    passes to revoke_link always comes from the caller's own resolved session,
    never from user input. This probes the guarantee at the layer where a
    future refactor could actually break it: the repository call itself.
    """
    token_a, _ = await service.create_link_token("tenant_a", "user_a", ())
    token_b, _ = await service.create_link_token("tenant_b", "user_b", ())
    await handle_message(_msg(f"/start {token_a}", user="42"), trace_id="t-1")
    await handle_message(_msg(f"/start {token_b}", user="43"), trace_id="t-2")

    link_a = await repo.get_active_link("telegram", "42")
    assert link_a is not None

    revoked = await repo.revoke_link(link_a["id"], "tenant_b")

    assert revoked is False
    assert await repo.get_active_link("telegram", "42") is not None
    ctx = await service.resolve_session("telegram", "42", "t-3")
    assert ctx is not None and ctx.tenant_id == "tenant_a"
