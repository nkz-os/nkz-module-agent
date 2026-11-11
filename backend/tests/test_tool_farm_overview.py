"""get_farm_overview: crop-health tenant work list in one call."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.domain.session import SessionContext
from app.tools.adapters import crop_health
from app.tools.catalog import available_tools


def _ctx() -> SessionContext:
    return SessionContext("acme", "u", ("Farmer", "TenantAdmin"), "mobile", "1", "t")


class _Response:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "e",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _install(monkeypatch, body, status=200, url="http://crop-health.invalid"):
    box = {"calls": 0}

    class _Client:
        def __init__(self, timeout=None):
            box["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, u, headers=None, params=None):
            box.update(url=u, headers=headers, params=params)
            box["calls"] += 1
            return _Response(body, status)

    monkeypatch.setattr(crop_health.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        crop_health,
        "get_settings",
        lambda: SimpleNamespace(crop_health_url=url, hmac_secret="s"),
    )
    return box


def _row(i, **kw):
    return {"parcelId": f"p{i}", "parcelName": f"Parcel {i}", "hasData": True, **kw}


_ARGS = crop_health.GetFarmOverviewArgs


@pytest.mark.asyncio
async def test_happy_mapping_headers_and_citation(monkeypatch):
    box = _install(monkeypatch, {
        "parcels": [
            _row(1, cropName="Wheat", phenologyStage="tillering", areaHa=3.5,
                 overallSeverity="HIGH", cwsiValue=0.6, vigorIndex=0.4,
                 assessedAt="2026-11-01T04:00:00Z", surprise={"x": 1},
                 nextJob={"kind": "irrigate", "when": "today",
                          "detail": {"mm": 12}}),
            _row(2, cropName="Barley", overallSeverity="LOW",
                 nextJob={"kind": "pending_assessment", "when": None}),
            {"parcelId": "p3", "parcelName": "Parcel 3", "hasData": False},
        ],
        "summary": {"surprises": 1, "jobsToday": 1, "noData": 1},
    })
    res = await crop_health.get_farm_overview(_ctx(), _ARGS())
    assert res.ok is True
    p = res.payload
    assert p["truncated"] is False
    assert p["summary"] == {"surprises": 1, "jobs_today": 1, "no_data": 1}
    first, second, third = p["parcels"]
    assert first["parcel_id"] == "urn:ngsi-ld:AgriParcel:p1" and first["name"] == "Parcel 1"
    assert first["crop"] == "Wheat" and first["stage"] == "tillering"
    assert first["area_ha"] == 3.5 and first["severity"] == "HIGH"
    assert first["cwsi"] == 0.6 and first["vigor"] == 0.4
    assert first["assessed_at"] == "2026-11-01T04:00:00Z"
    assert first["has_data"] is True
    assert first["next_job"]["kind"] == "irrigate"
    assert first["next_job"]["when"] == "today"
    assert first["next_job"]["detail"] == {"mm": 12}
    assert second["next_job"]["kind"] == "pending_assessment"
    assert "when" not in second["next_job"]
    assert third["has_data"] is False
    assert "next_job" not in third and "crop" not in third
    assert "severity" not in third
    assert box["url"] == "http://crop-health.invalid/api/crop-health/parcels"
    assert not box["params"]
    assert set(box["headers"]) == {
        "X-Tenant-ID", "X-User-ID", "X-User-Roles", "X-Auth-Signature"}
    assert box["headers"]["X-Tenant-ID"] == "acme"
    assert box["headers"]["X-User-Roles"] == "Farmer,TenantAdmin"
    assert res.citations[0].source == "crop-health:parcels"
    assert res.citations[0].detail == "tenant work list"


@pytest.mark.asyncio
async def test_urn_parcel_id_is_not_prefixed_twice(monkeypatch):
    _install(monkeypatch, {"parcels": [
        _row(1) | {"parcelId": "urn:ngsi-ld:AgriParcel:abc"}], "summary": {}})
    res = await crop_health.get_farm_overview(_ctx(), _ARGS())
    assert res.payload["parcels"][0]["parcel_id"] == "urn:ngsi-ld:AgriParcel:abc"


@pytest.mark.asyncio
async def test_truncates_beyond_cap(monkeypatch):
    _install(monkeypatch, {"parcels": [_row(i) for i in range(41)],
                           "summary": {}})
    res = await crop_health.get_farm_overview(_ctx(), _ARGS())
    assert res.ok is True
    assert len(res.payload["parcels"]) == 40
    assert res.payload["truncated"] is True
    assert res.payload["summary"] == {"surprises": 0, "jobs_today": 0, "no_data": 0}


@pytest.mark.asyncio
async def test_exactly_cap_not_truncated(monkeypatch):
    _install(monkeypatch, {"parcels": [_row(i) for i in range(40)],
                           "summary": {}})
    res = await crop_health.get_farm_overview(_ctx(), _ARGS())
    assert len(res.payload["parcels"]) == 40
    assert res.payload["truncated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    [], "x", {"parcels": "nope"}, {"parcels": [{"parcelName": "no id"}]},
    {"parcels": [], "summary": []}, ValueError("bad json"),
])
async def test_malformed_body_fails_closed(monkeypatch, body):
    _install(monkeypatch, body)
    res = await crop_health.get_farm_overview(_ctx(), _ARGS())
    assert res.ok is False
    assert res.unavailable_reason == "crop health unreachable"
    assert res.payload is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500])
async def test_http_error_fails_closed(monkeypatch, status):
    _install(monkeypatch, {"parcels": []}, status=status)
    res = await crop_health.get_farm_overview(_ctx(), _ARGS())
    assert res.ok is False
    assert res.unavailable_reason == "crop health unreachable"


@pytest.mark.asyncio
async def test_empty_url_no_http(monkeypatch):
    box = _install(monkeypatch, {"parcels": []}, url="")
    res = await crop_health.get_farm_overview(_ctx(), _ARGS())
    assert res.ok is False
    assert box["calls"] == 0


def test_tool_definition_registered():
    tools = {t.name: t for t in available_tools()}
    t = tools["get_farm_overview"]
    assert t.required_module == "crop-health"
    assert t.required_setting == "crop_health_url"
    assert t.timeout_s == 15.0
    assert t.description == (
        "One-call overview of every parcel of the holding: name, crop, "
        "phenological stage, overall severity, water stress (CWSI), vigour, "
        "assessment date and the next job due. Use this for whole-farm "
        "questions instead of reading parcels one by one."
    )
    names = [x.name for x in available_tools()]
    assert names.index("get_farm_overview") == names.index("get_parcels") + 1


def test_parcel_state_description_hints_overview():
    t = {x.name: x for x in available_tools()}["get_parcel_state"]
    assert t.description.endswith(
        " For several parcels at once, prefer get_farm_overview.")
