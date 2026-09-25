import pytest

from app.agent.quota import check_and_count
from app.config import get_settings
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def _seed(db_pool, n, tenant="tenant_a", acct="42", hours_ago=0):
    async with db_pool.acquire() as c:
        for i in range(n):
            await c.execute(
                """INSERT INTO agent_turn_audit
                   (trace_id, tenant_id, user_id, channel, channel_user_id,
                    outcome, created_at)
                   VALUES ($1,$2,'u','telegram',$3,'ok', now() - ($4 || ' hours')::interval)""",
                f"seed-{tenant}-{acct}-{i}", tenant, acct, str(hours_ago),
            )


async def test_allows_when_under_both_caps(db_pool):
    assert await check_and_count("tenant_a", "telegram", "42") is None


async def test_blocks_when_tenant_daily_cap_reached(db_pool, monkeypatch):
    monkeypatch.setenv("MAX_TURNS_PER_TENANT_DAY", "3")
    get_settings.cache_clear()
    await _seed(db_pool, 3)
    assert await check_and_count("tenant_a", "telegram", "99") == "tenant_daily"
    get_settings.cache_clear()


async def test_blocks_when_account_hourly_cap_reached(db_pool, monkeypatch):
    monkeypatch.setenv("MAX_TURNS_PER_ACCOUNT_HOUR", "2")
    get_settings.cache_clear()
    await _seed(db_pool, 2)
    assert await check_and_count("tenant_a", "telegram", "42") == "account_hourly"
    get_settings.cache_clear()


async def test_old_turns_do_not_count_against_the_hourly_cap(db_pool, monkeypatch):
    monkeypatch.setenv("MAX_TURNS_PER_ACCOUNT_HOUR", "2")
    get_settings.cache_clear()
    await _seed(db_pool, 5, hours_ago=5)
    assert await check_and_count("tenant_a", "telegram", "42") is None
    get_settings.cache_clear()


async def test_one_tenant_cannot_exhaust_anothers_quota(db_pool, monkeypatch):
    """Counting without a tenant filter would let one tenant deny service to all."""
    monkeypatch.setenv("MAX_TURNS_PER_TENANT_DAY", "3")
    get_settings.cache_clear()
    await _seed(db_pool, 5, tenant="tenant_b")
    assert await check_and_count("tenant_a", "telegram", "42") is None
    get_settings.cache_clear()


# --- Reinforcements: both sides of each boundary ---------------------------------


async def test_allows_at_tenant_daily_cap_minus_one(db_pool, monkeypatch):
    monkeypatch.setenv("MAX_TURNS_PER_TENANT_DAY", "3")
    get_settings.cache_clear()
    await _seed(db_pool, 2)
    assert await check_and_count("tenant_a", "telegram", "42") is None
    get_settings.cache_clear()


async def test_allows_at_account_hourly_cap_minus_one(db_pool, monkeypatch):
    monkeypatch.setenv("MAX_TURNS_PER_ACCOUNT_HOUR", "2")
    get_settings.cache_clear()
    await _seed(db_pool, 1)
    assert await check_and_count("tenant_a", "telegram", "42") is None
    get_settings.cache_clear()


async def test_tenant_daily_only_blocks_that_tenant(db_pool, monkeypatch):
    """The tenant cap is per tenant: an exhausted tenant_a never blocks tenant_b."""
    monkeypatch.setenv("MAX_TURNS_PER_TENANT_DAY", "3")
    get_settings.cache_clear()
    await _seed(db_pool, 3)
    assert await check_and_count("tenant_b", "telegram", "42") is None
    get_settings.cache_clear()


async def test_account_hourly_counts_across_tenants(db_pool, monkeypatch):
    """The hourly account budget is per human, not per (tenant, human):
    the same channel_user_id gets one rolling hourly budget even when talking
    to two different tenants."""
    monkeypatch.setenv("MAX_TURNS_PER_ACCOUNT_HOUR", "2")
    get_settings.cache_clear()
    await _seed(db_pool, 2, tenant="tenant_b")
    assert await check_and_count("tenant_a", "telegram", "42") == "account_hourly"
    get_settings.cache_clear()


async def test_tenant_daily_counts_all_channels(db_pool, monkeypatch):
    """The tenant cap is per tenant overall: Telegram and WhatsApp turns share it."""
    monkeypatch.setenv("MAX_TURNS_PER_TENANT_DAY", "3")
    get_settings.cache_clear()
    async with db_pool.acquire() as c:
        for i in range(3):
            await c.execute(
                """INSERT INTO agent_turn_audit
                   (trace_id, tenant_id, user_id, channel, channel_user_id,
                    outcome, created_at)
                   VALUES ($1,'tenant_a','u','whatsapp','w1','ok', now())""",
                f"seed-wa-{i}",
            )
    assert await check_and_count("tenant_a", "telegram", "42") == "tenant_daily"
    get_settings.cache_clear()


async def test_both_caps_reached_reports_account_hourly(db_pool, monkeypatch):
    """Precedence is deterministic: when both caps are hit at once, the
    implementation reports 'account_hourly' (account check runs first)."""
    monkeypatch.setenv("MAX_TURNS_PER_TENANT_DAY", "3")
    monkeypatch.setenv("MAX_TURNS_PER_ACCOUNT_HOUR", "2")
    get_settings.cache_clear()
    await _seed(db_pool, 3)  # 3 turns today by account 42 -> both caps hit
    assert await check_and_count("tenant_a", "telegram", "42") == "account_hourly"
    get_settings.cache_clear()


async def test_check_and_count_is_check_only(db_pool, monkeypatch):
    """Review 8 minor 2: a blocked turn is refused, never counted -- the check
    must not insert or mutate any audit row."""
    monkeypatch.setenv("MAX_TURNS_PER_ACCOUNT_HOUR", "2")
    get_settings.cache_clear()
    await _seed(db_pool, 2)
    async with db_pool.acquire() as c:
        before = await c.fetchval("SELECT count(*) FROM agent_turn_audit")

    assert await check_and_count("tenant_a", "telegram", "42") == "account_hourly"
    assert await check_and_count("tenant_a", "telegram", "42") == "account_hourly"

    async with db_pool.acquire() as c:
        after = await c.fetchval("SELECT count(*) FROM agent_turn_audit")
    assert after == before
    get_settings.cache_clear()


async def test_tenant_daily_window_expires(db_pool, monkeypatch):
    """Review 8 minor 3: rows older than one day must not count against the
    tenant daily cap, even when the count is at the cap."""
    monkeypatch.setenv("MAX_TURNS_PER_TENANT_DAY", "3")
    get_settings.cache_clear()
    await _seed(db_pool, 3, hours_ago=25)
    assert await check_and_count("tenant_a", "telegram", "42") is None
    get_settings.cache_clear()


async def test_account_hourly_cap_is_scoped_by_channel(db_pool, monkeypatch):
    """Review 8 minor 4: the hourly account cap is per channel, so the same
    human on a second channel keeps its own rolling budget."""
    monkeypatch.setenv("MAX_TURNS_PER_ACCOUNT_HOUR", "2")
    get_settings.cache_clear()
    await _seed(db_pool, 2)  # telegram / 42 at the cap
    assert await check_and_count("tenant_a", "whatsapp", "42") is None
    # The exhausted telegram channel still blocks, proving the isolation.
    assert await check_and_count("tenant_a", "telegram", "42") == "account_hourly"
    get_settings.cache_clear()
