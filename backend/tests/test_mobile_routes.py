from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from app.agent import quota
from app.api import mobile
from app.config import get_settings
from app.main import create_app
from app.middleware import AuthContext, get_current_user
from app.mobile import turns

pytestmark = pytest.mark.asyncio

URN = "urn:ngsi-ld:AgriParcel:p1"
NOW = datetime.now(timezone.utc)


def _turn(**kw):
    base = dict(
        turn_id="11111111-1111-1111-1111-111111111111", tenant_id="tenant-a",
        user_id="u1", client_turn_id="c1", parcel_id=None, lang="es",
        text="hola", state="queued", reply_text=None, citations=[],
        outcome=None, error_code=None, trace_id="tr", created_at=datetime.now(timezone.utc),
        started_at=None, finished_at=None,
    )
    base.update(kw)
    return turns.MobileTurn(**base)


class FakeOrion:
    entity: object = {"id": URN, "name": {"type": "Property", "value": "Olivar"}}
    boom = False

    def __init__(self, tenant):
        FakeOrion.tenant = tenant

    async def get_entity(self, urn):
        if FakeOrion.boom:
            raise RuntimeError("broker down")
        return FakeOrion.entity

    async def close(self):
        pass


@pytest.fixture
def state(monkeypatch):
    st = {"scheduled": [], "quota_calls": 0, "created": []}

    async def get_by_client(t, u, c):
        return st.get("existing")

    async def has_open(t, u):
        return st.get("open", False)

    async def check(t, ch, u):
        st["quota_calls"] += 1
        return st.get("blocked")

    async def slot(t, ch, u):
        return datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    async def create(t, u, c, text, parcel, lang, trace):
        st["created"].append((t, u, c, text, parcel, lang))
        return _turn(text=text, parcel_id=parcel), st.get("is_new", True)

    async def get(t, u, turn_id):
        return st.get("row") if (t, u) == ("tenant-a", "u1") else None

    async def failed(turn_id, code, outcome):
        st["failed"] = (turn_id, code)
        st["row"] = _turn(state="failed", error_code=code, finished_at=NOW)

    async def history(t, u, limit):
        st["limit"] = limit
        return [_turn(), _turn(client_turn_id="c0")]

    monkeypatch.setattr(turns, "get_by_client_turn_id", get_by_client)
    monkeypatch.setattr(turns, "has_open_turn", has_open)
    monkeypatch.setattr(turns, "create_turn", create)
    monkeypatch.setattr(turns, "get_turn", get)
    monkeypatch.setattr(turns, "mark_failed", failed)
    monkeypatch.setattr(turns, "list_history", history)
    monkeypatch.setattr(quota, "check_and_count", check)
    monkeypatch.setattr(quota, "next_slot", slot)
    monkeypatch.setattr(mobile, "OrionClient", FakeOrion)
    FakeOrion.boom = False
    FakeOrion.entity = {"id": URN, "name": {"type": "Property", "value": "Olivar"}}

    async def run(*a):
        pass

    monkeypatch.setattr(mobile, "run_mobile_turn", run)
    # Capture scheduling without running the task.
    from fastapi import BackgroundTasks
    orig = BackgroundTasks.add_task

    def add(self, func, *a, **k):
        st["scheduled"].append((func, a))
        return orig(self, func, *a, **k)

    monkeypatch.setattr(BackgroundTasks, "add_task", add)
    return st


@pytest_asyncio.fixture
async def client(state):
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        tenant_id="tenant-a", user_id="u1", roles=["Farmer"]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    get_settings.cache_clear()


def body(**kw):
    return {"text": "hola", "client_turn_id": "c1", **kw}


P = "/api/agent/mobile"


async def test_create_schedules_background_run(client, state):
    r = await client.post(f"{P}/turns", json=body())
    assert r.status_code == 202
    assert r.json() == {"turn_id": "11111111-1111-1111-1111-111111111111", "state": "queued"}
    assert len(state["scheduled"]) == 1
    _, args = state["scheduled"][0]
    assert args[0] == r.json()["turn_id"] and args[2] == "hola" and args[3] is None
    assert args[1].channel == "mobile" and args[1].user_id == "u1"
    assert state["quota_calls"] == 1


async def test_identity_comes_from_auth_not_body(client, state):
    await client.post(f"{P}/turns", json=body(tenant_id="evil", user_id="evil"))
    assert state["created"][0][:2] == ("tenant-a", "u1")


async def test_repeat_returns_existing_without_run_or_quota(client, state):
    state["existing"] = _turn(state="running")
    state["open"] = True  # its own open turn must not 409 a retry
    r = await client.post(f"{P}/turns", json=body())
    assert r.status_code == 202 and r.json()["state"] == "running"
    assert state["scheduled"] == [] and state["quota_calls"] == 0
    assert state["created"] == []


async def test_race_loser_does_not_schedule(client, state):
    state["is_new"] = False
    r = await client.post(f"{P}/turns", json=body())
    assert r.status_code == 202 and state["scheduled"] == []


async def test_empty_text(client):
    r = await client.post(f"{P}/turns", json=body(text="   "))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "empty_text"


async def test_text_too_long(client):
    r = await client.post(f"{P}/turns", json=body(text="x" * 2001))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "text_too_long"
    assert (await client.post(f"{P}/turns", json=body(text="x" * 2000))).status_code == 202


async def test_parcel_bad_format(client):
    r = await client.post(f"{P}/turns", json=body(parcel_id="abc"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_parcel"


async def test_parcel_not_in_tenant(client):
    FakeOrion.entity = None
    r = await client.post(f"{P}/turns", json=body(parcel_id=URN))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "invalid_parcel"


async def test_parcel_broker_error_fails_closed(client, state):
    FakeOrion.boom = True
    r = await client.post(f"{P}/turns", json=body(parcel_id=URN))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "invalid_parcel"
    assert state["scheduled"] == []


async def test_parcel_context_uses_entity_name_and_tenant(client, state):
    r = await client.post(f"{P}/turns", json=body(parcel_id=URN))
    assert r.status_code == 202
    assert state["scheduled"][0][1][3] == f"Parcela en contexto: Olivar ({URN})"
    assert FakeOrion.tenant == "tenant-a"


async def test_parcel_name_falls_back_to_urn(client, state):
    FakeOrion.entity = {"id": URN}
    await client.post(f"{P}/turns", json=body(parcel_id=URN))
    assert state["scheduled"][0][1][3] == f"Parcela en contexto: {URN} ({URN})"


async def test_open_turn_409(client, state):
    state["open"] = True
    r = await client.post(f"{P}/turns", json=body())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "turn_in_progress"
    assert state["quota_calls"] == 0


async def test_quota_blocked_429_with_retry_at(client, state):
    state["blocked"] = "account_hourly"
    r = await client.post(f"{P}/turns", json=body())
    d = r.json()["detail"]
    assert r.status_code == 429 and d["code"] == "quota_exhausted"
    assert d["retry_at"] == "2026-01-01T12:00:00+00:00"
    assert state["scheduled"] == [] and state["created"] == []


async def test_get_turn_view(client, state):
    state["row"] = _turn(
        state="done", reply_text="r", outcome="ok", finished_at=NOW,
        citations=[{"source": "orion", "retrieved_at": "t", "detail": "d"}],
    )
    r = await client.get(f"{P}/turns/{state['row'].turn_id}")
    v = r.json()
    assert r.status_code == 200 and v["text"] == "hola" and v["reply_text"] == "r"
    assert v["citations"] == [{"source": "orion", "retrieved_at": "t", "detail": "d"}]
    assert set(v) == {
        "turn_id", "state", "text", "reply_text", "citations", "outcome",
        "error_code", "parcel_id", "created_at", "finished_at",
    }


async def test_get_other_users_turn_404(client, state):
    state["row"] = _turn()
    app_user = client._transport.app.dependency_overrides
    app_user[get_current_user] = lambda: AuthContext(
        tenant_id="tenant-a", user_id="u2", roles=["Farmer"]
    )
    r = await client.get(f"{P}/turns/{state['row'].turn_id}")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "turn_not_found"


async def test_get_stale_running_is_failed_interrupted(client, state):
    old = NOW - timedelta(seconds=45 + 15 + 5)
    state["row"] = _turn(state="running", created_at=old)
    r = await client.get(f"{P}/turns/{state['row'].turn_id}")
    assert r.json()["state"] == "failed" and r.json()["error_code"] == "interrupted"
    assert state["failed"][1] == "interrupted"


async def test_get_fresh_running_untouched(client, state):
    state["row"] = _turn(state="running")
    r = await client.get(f"{P}/turns/{state['row'].turn_id}")
    assert r.json()["state"] == "running" and "failed" not in state


async def test_history_limit_clamped(client, state):
    r = await client.get(f"{P}/history?limit=500")
    assert r.status_code == 200 and len(r.json()["turns"]) == 2
    assert state["limit"] == 50
    await client.get(f"{P}/history?limit=7")
    assert state["limit"] == 7


async def test_unauthenticated_is_401(state):
    get_settings.cache_clear()
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.post(f"{P}/turns", json=body())).status_code == 401
        assert (await c.get(f"{P}/history")).status_code == 401
