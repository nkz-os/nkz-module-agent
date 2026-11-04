from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

from app.config import get_settings
from app.db import get_pool
from app.main import create_app
from tests.conftest import requires_db
from tests.test_llm_config_routes import _role_overrides

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def client(db_pool):
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides.update(_role_overrides(app))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


def _ts(day, hour=12):
    n = datetime.now(timezone.utc)
    return datetime(n.year, n.month, day, hour, tzinfo=timezone.utc)


async def _row(tenant, day, *, channel="telegram", outcome="ok", model="m/known",
               tp=10, tc=5, hour=12):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant',$1,true)", tenant)
        await conn.execute(
            """INSERT INTO agent_turn_audit (trace_id, tenant_id, user_id, channel,
               channel_user_id, model, tokens_prompt, tokens_completion, outcome,
               created_at) VALUES ('t',$1,'u',$2,'c',$3,$4,$5,$6,$7)""",
            tenant, channel, model, tp, tc, outcome, _ts(day, hour),
        )


@pytest.fixture
def prices(monkeypatch):
    def estimate(model, prompt_tokens, completion_tokens):
        if model == "m/unknown":
            return None
        return prompt_tokens * 0.001 + completion_tokens * 0.002

    monkeypatch.setattr("app.llm.provider.estimate_cost", estimate)


async def test_empty_month(client):
    j = (await client.get("/api/agent/usage")).json()
    n = datetime.now(timezone.utc)
    assert j["month"] == f"{n.year:04d}-{n.month:02d}"
    assert j["days"] == []
    assert j["totals"]["turns"] == 0 and j["totals"]["cost_estimate_usd"] == 0.0


async def test_two_days_aggregate(client, prices):
    await _row("tenant-a", 1)
    await _row("tenant-a", 1, tp=20, tc=10)
    await _row("tenant-a", 2, tp=100, tc=0)
    await _row("tenant-b", 1, tp=999, tc=999)  # other tenant
    j = (await client.get("/api/agent/usage")).json()
    d1, d2 = j["days"]
    assert d1["date"].endswith("-01") and d2["date"].endswith("-02")
    assert (d1["turns"], d1["tokens_prompt"], d1["tokens_completion"]) == (2, 30, 15)
    assert d1["cost_estimate_usd"] == pytest.approx(30 * 0.001 + 15 * 0.002)
    assert d2["cost_estimate_usd"] == pytest.approx(0.1)
    assert j["totals"]["turns"] == 3 and j["totals"]["tokens_prompt"] == 130
    assert j["totals"]["cost_estimate_usd"] == pytest.approx(0.06 + 0.1)


async def test_refused_and_panel_not_turns_but_panel_tokens_count(client, prices):
    await _row("tenant-a", 1)
    await _row("tenant-a", 1, outcome="refused", tp=0, tc=0)
    await _row("tenant-a", 1, channel="panel", tp=9, tc=3)
    d = (await client.get("/api/agent/usage")).json()["days"][0]
    assert d["turns"] == 1
    assert d["tokens_prompt"] == 19 and d["tokens_completion"] == 8


async def test_unknown_model_gives_null_cost(client, prices):
    await _row("tenant-a", 1, model="m/unknown")
    await _row("tenant-a", 2)
    await _row("tenant-a", 3, model="m/known")
    await _row("tenant-a", 3, model="m/unknown")  # one unknown poisons the day
    j = (await client.get("/api/agent/usage")).json()
    costs = {d["date"][-2:]: d["cost_estimate_usd"] for d in j["days"]}
    assert costs["01"] is None and costs["03"] is None and costs["02"] is not None
    assert j["totals"]["cost_estimate_usd"] is None
    assert j["totals"]["turns"] == 4


async def test_previous_month_excluded(client, prices):
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant','tenant-a',true)")
        await conn.execute(
            """INSERT INTO agent_turn_audit (trace_id, tenant_id, user_id, channel,
               channel_user_id, model, tokens_prompt, tokens_completion, outcome,
               created_at) VALUES ('t','tenant-a','u','telegram','c','m/known',1,1,'ok',
               date_trunc('month', now()) - interval '1 day')""")
    assert (await client.get("/api/agent/usage")).json()["days"] == []
