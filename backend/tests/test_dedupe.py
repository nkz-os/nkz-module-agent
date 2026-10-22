import asyncio

import pytest

from app.dedupe import claim_update, purge_expired
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_first_claim_wins(db_pool):
    assert await claim_update("telegram:1") is True


async def test_repeat_claim_is_refused(db_pool):
    await claim_update("telegram:2")
    assert await claim_update("telegram:2") is False


async def test_concurrent_claims_yield_exactly_one_winner(db_pool):
    """A platform retry can arrive while the first is still in flight."""
    results = await asyncio.gather(*(claim_update("telegram:3") for _ in range(10)))
    assert sum(results) == 1


async def test_purge_removes_only_expired_rows(db_pool):
    await claim_update("telegram:4")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_processed_updates SET seen_at = now() - interval '48 hours' "
            "WHERE idempotency_key = 'telegram:4'"
        )
    await claim_update("telegram:5")

    assert await purge_expired() == 1
    assert await claim_update("telegram:5") is False
