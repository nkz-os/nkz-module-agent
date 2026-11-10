"""get_parcel_state: mobile-api briefing mapped to a frozen payload."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.domain.session import SessionContext
from app.tools.adapters import mobile_api
from app.tools.catalog import available_tools

PARCEL = "urn:ngsi-ld:AgriParcel:p1"


def _ctx() -> SessionContext:
    return SessionContext(
        tenant_id="acme",
        user_id="user_a",
        roles=("Farmer", "TenantAdmin"),
        channel="mobile",
        channel_user_id="42",
        trace_id="t-1",
    )


class _Response:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise httpx.HTTPStatusError(
                "err", request=httpx.Request("GET", "http://x"), response=httpx.Response(self.status)
            )

    def json(self):
        return self._body


class _Client:
    def __init__(self, box, timeout=None):
        self.box = box
        box["timeout"] = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        self.box.update(url=url, params=params, headers=headers)
        self.box["calls"] = self.box.get("calls", 0) + 1
        return _Response(self.box["body"], self.box.get("status", 200))


def _install(monkeypatch, body, status=200, url="http://mobile.invalid"):
    box = {"body": body, "status": status}
    monkeypatch.setattr(mobile_api.httpx, "AsyncClient", lambda timeout=None: _Client(box, timeout))
    monkeypatch.setattr(
        mobile_api,
        "get_settings",
        lambda: SimpleNamespace(mobile_api_url=url, hmac_secret="test-secret"),
    )
    return box


BRIEFING = {
    "version": 1,
    "parcelId": "p1",
    "parcelName": "Norte",
    "sources": {"cropHealth": "ok", "weather": "failed", "soil": "empty"},
    "crop": {
        "status": "active", "species": "TRZAX", "name": "Trigo", "variety": "V1",
        "sowingDate": "2026-10-20", "provenance": "actual", "daysSinceSowing": 21,
        "plannedCrop": None, "internal": "x",
    },
    "campaign": {"stage": "tillering", "progressPct": 12.5, "deviation": None,
                 "gddAccumulated": 140.0, "dataFidelity": "parcel_weather"},
    "weather": {"now": None},
    "state": {
        "water": {"level": "ok", "messageKey": "w.ok", "params": {}},
        "plant": {"level": "warn", "messageKey": "p.warn"},
        "risks": {"level": "ok", "messageKey": "r.ok"},
        "overall": {"level": "warn", "messageKey": "o.warn"},
        "assessedAt": "2026-11-10T04:00:00Z", "freshness": "fresh",
    },
    "forecast": {"yield": {"value": 6.1, "unit": "t/ha", "low": 5.0, "high": 7.0},
                 "harvestDate": "2027-07-01", "computedAt": "x"},
    "nextJob": {"kind": "spraying", "when": "2026-11-12", "detail": {"a": 1}},
    "operations": [
        {"id": f"o{i}", "type": "sowing", "date": f"2026-10-{i + 1:02d}", "status": "completed"}
        for i in range(12)
    ],
}


@pytest.mark.asyncio
async def test_happy_path_maps_fields(monkeypatch):
    box = _install(monkeypatch, BRIEFING)
    res = await mobile_api.get_parcel_state(_ctx(), mobile_api.GetParcelStateArgs(parcel_id=PARCEL))
    assert res.ok is True
    p = res.payload
    assert box["url"] == "http://mobile.invalid/api/mobile/briefing/p1"
    assert box["params"] == {"lang": "es"}
    assert box["headers"]["X-User-Roles"] == "Farmer,TenantAdmin"
    assert {"X-Tenant-ID", "X-User-ID", "X-Auth-Signature"} <= set(box["headers"])
    assert box["timeout"] == 15.0
    assert p["crop"]["sowingDate"] == "2026-10-20" and p["crop"]["provenance"] == "actual"
    assert "internal" not in p["crop"]
    assert p["campaign"]["stage"] == "tillering"
    assert p["state"]["plant"] == {"level": "warn", "messageKey": "p.warn"}
    assert "params" not in p["state"]["water"]
    assert p["assessedAt"] == "2026-11-10T04:00:00Z" and p["freshness"] == "fresh"
    assert p["forecast"] == {
        "harvestDate": "2027-07-01",
        "yield": {"value": 6.1, "unit": "t/ha", "low": 5.0, "high": 7.0},
    }
    assert p["nextJob"]["kind"] == "spraying"
    assert len(p["operations"]) == 10
    assert set(p["operations"][0]) == {"type", "date", "status"}
    assert len(res.citations) == 1
    c = res.citations[0]
    assert c.source == "mobile-api:briefing" and c.detail == "parcel=p1"


@pytest.mark.asyncio
async def test_sources_passed_through(monkeypatch):
    _install(monkeypatch, BRIEFING)
    res = await mobile_api.get_parcel_state(_ctx(), mobile_api.GetParcelStateArgs(parcel_id=PARCEL))
    assert res.payload["sources"] == {
        "cropHealth": {"status": "ok"},
        "weather": {"status": "failed"},
        "soil": {"status": "empty"},
    }


@pytest.mark.asyncio
async def test_404_is_unreachable(monkeypatch):
    _install(monkeypatch, {"error": "parcel_not_found"}, status=404)
    res = await mobile_api.get_parcel_state(_ctx(), mobile_api.GetParcelStateArgs(parcel_id=PARCEL))
    assert res.ok is False and res.unavailable_reason == "parcel state unreachable"
    assert res.citations == ()


@pytest.mark.asyncio
async def test_malformed_body_without_crop(monkeypatch):
    _install(monkeypatch, {"sources": {}})
    res = await mobile_api.get_parcel_state(_ctx(), mobile_api.GetParcelStateArgs(parcel_id=PARCEL))
    assert res.ok is False and res.unavailable_reason == "parcel state unreachable"


@pytest.mark.asyncio
async def test_empty_url_makes_no_http_call(monkeypatch):
    box = _install(monkeypatch, BRIEFING, url="")
    res = await mobile_api.get_parcel_state(_ctx(), mobile_api.GetParcelStateArgs(parcel_id=PARCEL))
    assert res.ok is False and "calls" not in box


def test_registered_with_contract():
    tool = {t.name: t for t in available_tools()}["get_parcel_state"]
    assert tool.required_setting == "mobile_api_url" and tool.required_module is None
    assert tool.timeout_s == 15.0
