import asyncio
import hashlib

import asyncpg
import pytest

from app.identity import repository as repo
from app.identity import service
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest.fixture
def clear_settings_cache():
    """Clear get_settings()'s cache on teardown, even if the test fails.

    A trailing cache_clear() on a test's last line never runs once an
    earlier assertion raises, poisoning the cache for the rest of the
    session.
    """
    yield
    from app.config import get_settings

    get_settings.cache_clear()


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


async def test_deep_link_uses_the_configured_bot(db_pool, monkeypatch, clear_settings_cache):
    from app.config import get_settings
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "somebot")
    get_settings.cache_clear()

    token, link = await service.create_link_token("tenant_a", "user_a", ())
    assert link == f"https://t.me/somebot?start={token}"


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


async def test_create_link_token_uses_the_csprng_and_only_the_csprng(db_pool, monkeypatch):
    """Unpredictability is a property of the token's SOURCE, not observable
    from any finite sample of its output: two tokens differing, or a
    thousand tokens differing, is consistent with both a CSPRNG and a
    sufficiently clever deterministic function of tenant_id/user_id (e.g.
    a counter-based derivation) — no output-only assertion rules that out.
    Patching the CSPRNG and asserting the minted token IS its return value
    proves the token comes from nowhere else.
    """
    monkeypatch.setattr(service.secrets, "token_urlsafe", lambda n: "sentinel-token")

    token, _ = await service.create_link_token("tenant_a", "user_a", ())

    assert token == "sentinel-token"


async def test_tokens_minted_for_the_same_identity_are_unique(db_pool):
    """Alongside the CSPRNG-source assertion above: catches a degenerate or
    seeded source that does call secrets.token_urlsafe but repeats.
    """
    tokens = {
        (await service.create_link_token("tenant_a", "user_a", ()))[0] for _ in range(50)
    }
    assert len(tokens) == 50


async def test_resolve_session_roles_are_a_tuple(db_pool):
    """resolve_session runs once per inbound message — the hot path where
    the frozen SessionContext must actually hold a tuple, not the list
    the repository returns.
    """
    token, _ = await service.create_link_token("tenant_a", "user_a", ("Farmer", "Admin"))
    await service.redeem_link_token(token, "telegram", "42")

    ctx = await service.resolve_session("telegram", "42", "t-1")

    assert ctx is not None
    assert isinstance(ctx.roles, tuple)
    assert ctx.roles == ("Farmer", "Admin")


async def test_expired_token_is_refused(db_pool, monkeypatch, clear_settings_cache):
    """Of unknown / expired / already-consumed / tampered, only 'expired'
    was untested.
    """
    from app.config import get_settings

    monkeypatch.setenv("LINK_TOKEN_TTL_SECONDS", "-1")
    get_settings.cache_clear()

    token, _ = await service.create_link_token("tenant_a", "user_a", ())

    assert await service.redeem_link_token(token, "telegram", "42") is None


async def test_redeem_is_single_use_under_concurrent_redemption(db_pool):
    """Sequential redemption already proves single-use; this exercises the
    same guarantee under a race, at the layer that actually calls the
    repository's atomic single-use update.
    """
    token, _ = await service.create_link_token("tenant_a", "user_a", ())

    results = await asyncio.gather(
        *(service.redeem_link_token(token, "telegram", str(n)) for n in range(10)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert exceptions == [], f"concurrent redemption raised: {exceptions!r}"

    winners = [r for r in results if r is not None]
    assert len(winners) == 1


async def test_redeem_retries_once_on_a_concurrent_relink_race(db_pool, monkeypatch):
    """Two concurrent redemptions for the same channel account can both pass
    upsert_active_link's revoke step before either commits its insert; the
    partial unique index then rejects the loser's INSERT with
    UniqueViolationError. By the time this runs, the loser's token is
    already consumed (single-use, atomic, and irreversible) — surfacing
    that raw error would both 500 the caller and strand them with a burned
    credential. redeem_link_token must retry once instead: the retry
    re-runs revoke-then-insert, which is exactly what a manual relink
    already does (last writer wins).

    Real concurrent redemptions were tried here and found unreliable to
    reproduce on demand and, past two simultaneous racers, capable of
    exhausting a single retry regardless of implementation — this
    deterministically forces the one-collision case the fix targets.
    """
    token, _ = await service.create_link_token("tenant_a", "user_a", ())

    real_upsert = repo.upsert_active_link
    calls = {"count": 0}

    async def flaky_upsert(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise asyncpg.UniqueViolationError("simulated concurrent relink")
        return await real_upsert(*args, **kwargs)

    monkeypatch.setattr(service.repo, "upsert_active_link", flaky_upsert)

    ctx = await service.redeem_link_token(token, "telegram", "42")

    assert calls["count"] == 2, "must retry exactly once, not loop or give up"
    assert ctx is not None
    assert ctx.tenant_id == "tenant_a"

    link = await repo.get_active_link("telegram", "42")
    assert link is not None, "the retried insert must have actually landed"
