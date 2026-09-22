import hashlib

import pytest

from app.identity import service
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_created_token_is_never_stored_in_clear(db_pool):
    token, _ = await service.create_link_token("tenant_a", "user_a", ("Farmer",))

    async with db_pool.acquire() as conn:
        stored = await conn.fetchval("SELECT token_hash FROM agent_link_tokens")

    assert stored != token
    assert stored == hashlib.sha256(token.encode()).hexdigest()


async def test_token_fits_the_telegram_start_payload_limit(db_pool):
    """Telegram caps the /start payload at 64 characters."""
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    assert len(token) <= 64
    assert token.replace("-", "").replace("_", "").isalnum()


async def test_deep_link_uses_the_configured_bot(db_pool, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "somebot")
    get_settings.cache_clear()

    token, link = await service.create_link_token("tenant_a", "user_a", ())
    assert link == f"https://t.me/somebot?start={token}"

    get_settings.cache_clear()


async def test_redeem_produces_a_session_for_that_identity(db_pool):
    token, _ = await service.create_link_token("tenant_a", "user_a", ("Farmer",))

    ctx = await service.redeem_link_token(token, "telegram", "42")

    assert ctx is not None
    assert (ctx.tenant_id, ctx.user_id, ctx.roles) == ("tenant_a", "user_a", ("Farmer",))
    assert ctx.channel_user_id == "42"


async def test_redeem_is_single_use(db_pool):
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    assert await service.redeem_link_token(token, "telegram", "42") is not None
    assert await service.redeem_link_token(token, "telegram", "43") is None


async def test_tampered_token_is_refused(db_pool):
    token, _ = await service.create_link_token("tenant_a", "user_a", ())
    assert await service.redeem_link_token(token[:-1] + "X", "telegram", "42") is None


async def test_resolve_session_returns_none_for_unlinked_account(db_pool):
    assert await service.resolve_session("telegram", "999", "t-1") is None


async def test_resolve_session_after_relink_never_returns_the_old_tenant(db_pool):
    t1, _ = await service.create_link_token("tenant_a", "user_a", ())
    await service.redeem_link_token(t1, "telegram", "42")

    t2, _ = await service.create_link_token("tenant_b", "user_b", ())
    await service.redeem_link_token(t2, "telegram", "42")

    ctx = await service.resolve_session("telegram", "42", "t-1")
    assert ctx is not None
    assert ctx.tenant_id == "tenant_b"
