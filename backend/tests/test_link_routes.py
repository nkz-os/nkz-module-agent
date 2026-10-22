"""These tests drive the ASGI app with an async client on purpose.

The routes and the db_pool fixture share one asyncio loop that way. A
synchronous TestClient runs the app in a loop of its own, so the connection
pool built inside a request would belong to a different loop than the
fixture's — which fails in confusing, intermittent ways.
"""

import httpx
import pytest
import pytest_asyncio
from nkz_platform_sdk.auth import AuthContext

from app.config import get_settings
from app.identity import repository as repo
from app.main import create_app
from app.middleware import get_current_user
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def client(monkeypatch, db_pool):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "somebot")
    get_settings.cache_clear()

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        tenant_id="tenant_a", user_id="user_a", roles=("Farmer",)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    get_settings.cache_clear()


async def test_create_link_token_returns_a_deep_link(client):
    r = await client.post("/api/agent/link-tokens")
    assert r.status_code == 200
    body = r.json()
    assert body["deep_link"].startswith("https://t.me/somebot?start=")
    assert body["expires_in"] == 600


async def test_response_never_exposes_the_raw_token_field(client):
    body = (await client.post("/api/agent/link-tokens")).json()
    assert "token" not in body


async def test_links_start_empty(client):
    r = await client.get("/api/agent/links")
    assert r.json() == {"links": []}


async def test_created_link_is_listed(client):
    await repo.upsert_active_link("telegram", "55", "tenant_a", "user_a", ("Farmer",))
    body = (await client.get("/api/agent/links")).json()
    assert [l["channel_user_id"] for l in body["links"]] == ["55"]


async def test_links_do_not_include_another_tenants_link(client):
    await repo.upsert_active_link("telegram", "55", "tenant_a", "user_a", ("Farmer",))
    await repo.upsert_active_link("telegram", "77", "tenant_other", "user_x", ())
    body = (await client.get("/api/agent/links")).json()
    assert [l["channel_user_id"] for l in body["links"]] == ["55"]


async def test_revoking_a_foreign_link_is_not_found(client):
    link_id = await repo.upsert_active_link(
        "telegram", "77", "tenant_other", "user_x", ()
    )
    r = await client.delete(f"/api/agent/links/{link_id}")
    assert r.status_code == 404
    assert await repo.get_active_link("telegram", "77") is not None
