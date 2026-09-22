from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.db import close_pool
from app.identity import repository as repo
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _reset_process_pool():
    # app.db.get_pool() is a process-wide singleton, but pytest-asyncio gives
    # each test its own event loop. An asyncpg pool is bound to the loop that
    # created it, so a pool left over from a previous test is unusable here.
    # Reset it around every test, exactly like test_db.py does for the same
    # reason.
    await close_pool()
    yield
    await close_pool()


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=10)


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=1)


async def test_consume_token_returns_identity_once(db_pool):
    await repo.insert_link_token("h1", "tenant_a", "user_a", ("Farmer",), _future())

    first = await repo.consume_link_token("h1")
    assert first == {"tenant_id": "tenant_a", "user_id": "user_a", "roles": ["Farmer"]}

    second = await repo.consume_link_token("h1")
    assert second is None, "a link token must be single-use"


async def test_expired_token_is_refused(db_pool):
    await repo.insert_link_token("h2", "tenant_a", "user_a", (), _past())
    assert await repo.consume_link_token("h2") is None


async def test_unknown_token_is_refused(db_pool):
    assert await repo.consume_link_token("nope") is None


async def test_relinking_revokes_the_previous_link(db_pool):
    await repo.upsert_active_link("telegram", "42", "tenant_a", "user_a", ("Farmer",))
    await repo.upsert_active_link("telegram", "42", "tenant_b", "user_b", ("Farmer",))

    link = await repo.get_active_link("telegram", "42")
    assert link is not None
    assert link["tenant_id"] == "tenant_b", "only the newest link may be active"

    async with db_pool.acquire() as conn:
        active = await conn.fetchval(
            "SELECT count(*) FROM agent_channel_links "
            "WHERE channel='telegram' AND channel_user_id='42' AND status='active'"
        )
    assert active == 1


async def test_get_active_link_ignores_revoked(db_pool):
    link_id = await repo.upsert_active_link("telegram", "43", "tenant_a", "user_a", ())
    assert await repo.revoke_link(link_id, "tenant_a") is True
    assert await repo.get_active_link("telegram", "43") is None


async def test_revoke_is_scoped_to_the_owning_tenant(db_pool):
    link_id = await repo.upsert_active_link("telegram", "44", "tenant_a", "user_a", ())
    assert await repo.revoke_link(link_id, "tenant_b") is False
    assert await repo.get_active_link("telegram", "44") is not None
