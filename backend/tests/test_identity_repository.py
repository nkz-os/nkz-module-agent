import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.identity import repository as repo
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


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


async def test_concurrent_redemption_yields_exactly_one_winner(db_pool):
    """Twenty callers race to redeem the same token; exactly one may win.

    A single-use guarantee that only holds under sequential access is not a
    guarantee at all: this is what stops a replayed or forwarded link token
    from binding more than one session to a tenant. A non-atomic
    SELECT-then-UPDATE implementation lets several callers see
    consumed_at IS NULL before any of them writes it.
    """
    await repo.insert_link_token("hc", "tenant_a", "user_a", ("Farmer",), _future())

    results = await asyncio.gather(
        *(repo.consume_link_token("hc") for _ in range(20)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert exceptions == [], f"concurrent redemption raised: {exceptions!r}"

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, "exactly one concurrent redemption must succeed"
    assert len(losers) == 19


async def test_relinking_revokes_the_previous_link(db_pool):
    """Checks what the repository did to each row, not a schema side effect.

    A count-based assertion here is vacuous: the partial unique index makes
    two active rows structurally impossible, and finding the new link active
    already excludes zero — so `count == 1` cannot fail given the schema
    invariant plus the preceding assertion. Reading the old row's actual
    status and revoked_at tests repository.py's behaviour, not schema.sql's
    constraint.
    """
    old_id = await repo.upsert_active_link("telegram", "42", "tenant_a", "user_a", ("Farmer",))
    new_id = await repo.upsert_active_link("telegram", "42", "tenant_b", "user_b", ("Farmer",))

    async with db_pool.acquire() as conn:
        old_row = await conn.fetchrow(
            "SELECT status, revoked_at FROM agent_channel_links WHERE id = $1", old_id
        )
        new_row = await conn.fetchrow(
            "SELECT status, tenant_id FROM agent_channel_links WHERE id = $1", new_id
        )

    assert old_row["status"] == "revoked"
    assert old_row["revoked_at"] is not None
    assert new_row["status"] == "active"
    assert new_row["tenant_id"] == "tenant_b"


async def test_upsert_rolls_back_the_revoke_when_the_insert_fails(db_pool):
    """The revoke and the insert are one transaction.

    If the insert fails after the revoke has run outside a transaction, the
    account is left with zero active links: a farmer silently unlinked. Here
    a NOT NULL violation (tenant_id=None) forces the insert to fail; the
    previous link must still be active afterwards.
    """
    link_id = await repo.upsert_active_link("telegram", "45", "tenant_a", "user_a", ())

    with pytest.raises(asyncpg.PostgresError):
        await repo.upsert_active_link("telegram", "45", None, "user_b", ())

    link = await repo.get_active_link("telegram", "45")
    assert link is not None
    assert link["id"] == link_id
    assert link["tenant_id"] == "tenant_a"


async def test_get_active_link_ignores_revoked(db_pool):
    link_id = await repo.upsert_active_link("telegram", "43", "tenant_a", "user_a", ())
    assert await repo.revoke_link(link_id, "tenant_a") is True
    assert await repo.get_active_link("telegram", "43") is None


async def test_revoke_is_scoped_to_the_owning_tenant(db_pool):
    link_id = await repo.upsert_active_link("telegram", "44", "tenant_a", "user_a", ())
    assert await repo.revoke_link(link_id, "tenant_b") is False
    assert await repo.get_active_link("telegram", "44") is not None


async def test_list_links_excludes_revoked_and_other_tenants(db_pool):
    active_id = await repo.upsert_active_link("telegram", "50", "tenant_a", "user_a", ())
    to_revoke_id = await repo.upsert_active_link("whatsapp", "51", "tenant_a", "user_a", ())
    await repo.revoke_link(to_revoke_id, "tenant_a")
    other_tenant_id = await repo.upsert_active_link("telegram", "52", "tenant_b", "user_a", ())

    links = await repo.list_links("tenant_a", "user_a")
    ids = {link["id"] for link in links}

    assert active_id in ids
    assert to_revoke_id not in ids
    assert other_tenant_id not in ids
