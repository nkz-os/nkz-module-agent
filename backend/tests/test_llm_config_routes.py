import json
import logging

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from nkz_platform_sdk.auth import AuthContext

from app.config import get_settings
from app.llm import configs
from app.main import create_app
from app.middleware import get_current_user
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

KEY = "sk-secret-KEY-9f8e7d6c"
BASE = "https://llm.example.test/v1"
ROLES = {"value": ("TenantAdmin",)}


@pytest_asyncio.fixture
async def client(monkeypatch, db_pool):
    monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "LLM_ENDPOINT_ALLOWLIST", f"openai@{BASE.rsplit('/v1', 1)[0]},provider-a"
    )
    monkeypatch.setenv("MAX_TOKENS_PER_TURN_CEILING", "4000")
    get_settings.cache_clear()
    ROLES["value"] = ("TenantAdmin",)
    app = create_app()
    app.dependency_overrides.update(_role_overrides(app))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-User-ID": "admin1"}
    ) as c:
        yield c
    get_settings.cache_clear()


def _role_overrides(app):
    """Replace the role dependency with a fixed identity that still enforces
    the TenantAdmin/PlatformAdmin check (so Farmer gets 403)."""
    from fastapi import HTTPException

    from app.api.llm_config import _admin

    def _dep():
        roles = ROLES["value"]
        if not {"TenantAdmin", "PlatformAdmin"} & set(roles):
            raise HTTPException(status_code=403, detail="forbidden")
        return AuthContext(tenant_id="tenant-a", user_id="admin1", roles=roles)

    return {_admin: _dep}


def body(**over):
    b = {"provider": "openai", "model": "m1", "api_base": BASE, "api_key": KEY,
         "max_tokens_per_turn": 500}
    b.update(over)
    return b


def err_code(r):
    return r.json()["detail"]["code"]


async def test_farmer_gets_403(client):
    ROLES["value"] = ("Farmer",)
    for method, url in (("GET", "/api/agent/llm-config"), ("GET", "/api/agent/usage"),
                        ("DELETE", "/api/agent/llm-config"),
                        ("POST", "/api/agent/llm-config/test")):
        assert (await client.request(method, url)).status_code == 403
    assert (await client.put("/api/agent/llm-config", json=body())).status_code == 403


async def test_get_unconfigured(client):
    r = await client.get("/api/agent/llm-config")
    assert r.status_code == 200
    j = r.json()
    assert j["configured"] is False and j["byok_available"] is True
    assert j["ceiling"] == 4000 and j["notice"] is None
    assert {"provider": "provider-a", "origin": None} in j["allowed"]


async def test_put_happy_path_and_get_never_leak_key(client):
    r = await client.put("/api/agent/llm-config", json=body())
    assert r.status_code == 200
    j = r.json()
    assert j["configured"] and j["provider"] == "openai" and j["model"] == "m1"
    assert j["key_last4"] == KEY[-4:] and j["max_tokens_per_turn"] == 500
    row = await configs.get("tenant-a")
    assert row.updated_by == "admin1"
    g = await client.get("/api/agent/llm-config")
    for text in (r.text, g.text):
        assert KEY not in text and "ciphertext" not in text
        assert "api_key" not in json.loads(text)


async def test_provider_normalized(client):
    r = await client.put("/api/agent/llm-config",
                         json=body(provider=" OpenAI ", api_key=KEY))
    assert r.status_code == 200 and r.json()["provider"] == "openai"
    r = await client.put("/api/agent/llm-config",
                         json=body(provider=" Provider-A", api_base=None, api_key=None))
    assert r.status_code == 422 and err_code(r) == "key_required"


async def test_byok_unavailable_503(client, monkeypatch):
    monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEYS", "")
    get_settings.cache_clear()
    r = await client.put("/api/agent/llm-config", json=body())
    assert r.status_code == 503 and err_code(r) == "byok_unavailable"
    assert (await client.get("/api/agent/llm-config")).json()["byok_available"] is False


@pytest.mark.parametrize("model", ["", "x" * 201, "a b", "a\tb", "a\nb", " m"])
async def test_invalid_model(client, model):
    r = await client.put("/api/agent/llm-config", json=body(model=model))
    assert r.status_code == 422 and err_code(r) == "invalid_model"


async def test_slash_model_allowed(client):
    r = await client.put("/api/agent/llm-config", json=body(model="org/name-1"))
    assert r.status_code == 200


@pytest.mark.parametrize("key", ["short", "  1234567  "])
async def test_invalid_key(client, key):
    r = await client.put("/api/agent/llm-config", json=body(api_key=key))
    assert r.status_code == 422 and err_code(r) == "invalid_key"
    assert key.strip() not in r.text or key.strip() == ""


async def test_endpoint_not_allowed(client):
    for over in ({"api_base": "http://orion-ld-service:1026"},
                 {"api_base": "http://169.254.169.254"},
                 {"provider": "unlisted", "api_base": None},
                 {"provider": "provider-a"}):  # bare entry forbids api_base
        r = await client.put("/api/agent/llm-config", json=body(**over))
        assert r.status_code == 422 and err_code(r) == "endpoint_not_allowed", over


async def test_budget_above_ceiling(client):
    r = await client.put("/api/agent/llm-config", json=body(max_tokens_per_turn=4001))
    assert r.status_code == 422 and err_code(r) == "budget_above_ceiling"


async def test_validation_order(client, monkeypatch):
    # invalid_model beats endpoint_not_allowed beats budget
    r = await client.put("/api/agent/llm-config", json=body(
        model="", api_base="http://orion-ld-service:1026", max_tokens_per_turn=99999))
    assert err_code(r) == "invalid_model"
    r = await client.put("/api/agent/llm-config", json=body(
        api_base="http://orion-ld-service:1026", max_tokens_per_turn=99999))
    assert err_code(r) == "endpoint_not_allowed"


async def test_first_save_without_key_is_key_required(client):
    r = await client.put("/api/agent/llm-config", json=body(api_key=None))
    assert r.status_code == 422 and err_code(r) == "key_required"


async def test_provider_change_with_empty_key_never_reuses_old_key(client):
    assert (await client.put("/api/agent/llm-config", json=body())).status_code == 200
    before = await configs.get("tenant-a")
    r = await client.put("/api/agent/llm-config",
                         json=body(provider="provider-a", api_base=None, api_key=""))
    assert r.status_code == 422 and err_code(r) == "key_required"
    after = await configs.get("tenant-a")
    assert after.provider == "openai"
    assert after.api_key_ciphertext == before.api_key_ciphertext


async def test_model_change_keeps_key(client):
    await client.put("/api/agent/llm-config", json=body())
    r = await client.put("/api/agent/llm-config", json=body(model="m2", api_key=None))
    assert r.status_code == 200 and r.json()["key_last4"] == KEY[-4:]


async def test_malformed_body_does_not_echo_key(client):
    r = await client.put("/api/agent/llm-config",
                         json=body(max_tokens_per_turn="lots"))
    assert r.status_code == 422 and KEY not in r.text
    assert err_code(r) == "invalid_body"
    r = await client.put("/api/agent/llm-config", json=[KEY])
    assert r.status_code == 422 and KEY not in r.text
    r = await client.put("/api/agent/llm-config", content=KEY,
                         headers={"content-type": "application/json"})
    assert r.status_code == 422 and KEY not in r.text


async def test_delete(client):
    await client.put("/api/agent/llm-config", json=body())
    assert (await client.delete("/api/agent/llm-config")).status_code == 204
    assert await configs.get("tenant-a") is None
    assert (await client.delete("/api/agent/llm-config")).status_code == 204


async def test_notice_blocked_after_allowlist_narrowed(client, monkeypatch):
    await client.put("/api/agent/llm-config", json=body())
    monkeypatch.setenv("LLM_ENDPOINT_ALLOWLIST", "provider-a")
    get_settings.cache_clear()
    j = (await client.get("/api/agent/llm-config")).json()
    assert j["configured"] is True and j["notice"] == "blocked"


async def test_tenant_comes_from_auth_not_body(client):
    r = await client.put("/api/agent/llm-config", json=body(tenant_id="tenant-b"))
    assert r.status_code == 200
    assert await configs.get("tenant-b") is None
    assert await configs.get("tenant-a") is not None


async def test_logs_never_contain_key(client, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    async def boom(**kw):
        raise RuntimeError(f"auth failed for {kw['api_key']}")

    monkeypatch.setattr("app.llm.provider.acompletion", boom)
    await client.put("/api/agent/llm-config", json=body())
    await client.put("/api/agent/llm-config", json=body(max_tokens_per_turn="x"))
    await client.put("/api/agent/llm-config", json=body(model=""))
    r = await client.post("/api/agent/llm-config/test")
    assert r.json()["ok"] is False
    assert KEY not in r.text
    assert KEY not in caplog.text


async def test_real_role_dependency_rejects_farmer(db_pool):
    app = create_app()  # no overrides: the real require_roles runs
    transport = httpx.ASGITransport(app=app)
    h = {"X-Tenant-ID": "tenant-a", "X-User-ID": "u1", "X-User-Roles": "Farmer"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        for method, url in (("GET", "/api/agent/llm-config"), ("GET", "/api/agent/usage"),
                            ("POST", "/api/agent/llm-config/test")):
            r = await c.request(method, url, headers=h)
            assert r.status_code in (401, 403), (url, r.status_code)
            assert r.status_code == 403 or "signature" in r.text.lower()
