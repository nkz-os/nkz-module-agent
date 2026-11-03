import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from app.agent import quota
from app.config import get_settings
from app.db import get_pool
from app.main import create_app
from tests.conftest import requires_db
from tests.test_llm_config_routes import KEY, _role_overrides, body

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def client(monkeypatch, db_pool):
    monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    monkeypatch.setenv("LLM_ENDPOINT_ALLOWLIST", "openai@https://llm.example.test")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides.update(_role_overrides(app))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-User-ID": "admin1"}
    ) as c:
        yield c
    get_settings.cache_clear()


def _fake(seen, text="SECRET MODEL OUTPUT"):
    async def fake(**kw):
        seen.update(kw)

        class M:
            content = text
            tool_calls = None

        class C:
            message = M()

        class U:
            prompt_tokens = 9
            completion_tokens = 3

        class R:
            choices = [C()]
            usage = U()

        return R()
    return fake


async def _audit():
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant','tenant-a',true)")
        return await conn.fetch("SELECT * FROM agent_turn_audit")


async def test_unconfigured_is_audited(client):
    r = await client.post("/api/agent/llm-config/test")
    assert r.json()["ok"] is False
    rows = await _audit()
    assert [x["outcome"] for x in rows] == ["unconfigured"]
    assert rows[0]["channel"] == "panel"


async def test_ok_writes_panel_row_without_text_and_skips_quota(client, monkeypatch):
    seen = {}
    monkeypatch.setattr("app.llm.provider.acompletion", _fake(seen))
    await client.put("/api/agent/llm-config", json=body())
    r = await client.post("/api/agent/llm-config/test")
    assert r.json() == {"ok": True}
    assert seen["max_tokens"] <= 16 and seen["api_key"] == KEY
    rows = await _audit()
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "panel" and row["channel_user_id"] == "admin1"
    assert row["tenant_id"] == "tenant-a" and row["outcome"] == "ok"
    assert row["inbound_text"] is None
    assert "SECRET MODEL OUTPUT" not in (row["reply_text"] or "")
    assert row["tokens_prompt"] == 9 and row["llm_origin"] == "tenant"
    assert row["model"] == "openai/m1"
    assert KEY not in str(dict(row))
    # not quota consumption: hourly/daily counts see nothing
    s = get_settings()
    monkeypatch.setattr(s, "max_turns_per_account_hour", 1)
    monkeypatch.setattr(s, "max_turns_per_tenant_day", 1)
    for _ in range(3):
        await client.post("/api/agent/llm-config/test")
    verdict = await quota.check_and_count("tenant-a", "telegram", "5")
    assert verdict is None


async def test_provider_error_is_sanitized_and_audited(client, monkeypatch):
    async def boom(**kw):
        raise RuntimeError(f"bad key {kw['api_key']}")
    monkeypatch.setattr("app.llm.provider.acompletion", boom)
    await client.put("/api/agent/llm-config", json=body())
    r = await client.post("/api/agent/llm-config/test")
    j = r.json()
    assert j["ok"] is False and KEY not in r.text and "redacted" in j["error"]
    rows = await _audit()
    assert rows[0]["outcome"] == "error" and rows[0]["llm_origin"] == "tenant"
    assert KEY not in str(dict(rows[0]))
