"""get_vegetation_trend: NDVI series from the vegetation module."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.domain.session import SessionContext
from app.tools.adapters import vegetation
from app.tools.catalog import available_tools

URN = "urn:ngsi-ld:AgriParcel:p1"


def _ctx() -> SessionContext:
    return SessionContext("acme", "u", ("Farmer",), "mobile", "1", "t")


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


def _install(monkeypatch, body, status=200, url="http://vegetation.invalid"):
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

    monkeypatch.setattr(vegetation.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        vegetation, "get_settings", lambda: SimpleNamespace(vegetation_url=url, hmac_secret="s")
    )
    return box


def _pts(n):
    return [{"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "mean": 0.5,
             "min": 0.1, "max": 0.9, "std_dev": 0.05} for i in range(n)]


@pytest.mark.asyncio
async def test_happy_request_and_payload(monkeypatch):
    box = _install(monkeypatch, {
        "entity_id": URN, "index_type": "NDVI", "months": 6,
        "data_points": [
            {"date": "2026-10-20", "mean": 0.62, "min": 0.3, "max": 0.8, "std_dev": 0.1},
            {"date": "2026-10-05", "mean": 0.55, "min": 0.2, "max": 0.7, "std_dev": 0.1},
        ],
        "summary": {"avg": 9.9, "min": 9.9, "max": 9.9, "count": 99},
    })
    res = await vegetation.get_vegetation_trend(
        _ctx(), vegetation.GetVegetationTrendArgs(parcel_id=URN)
    )
    assert res.ok is True
    p = res.payload
    assert p["index"] == "NDVI" and p["latest_date"] == "2026-10-20"
    assert [x["date"] for x in p["points"]] == ["2026-10-05", "2026-10-20"]
    assert p["points"][0] == {"date": "2026-10-05", "mean": 0.55, "min": 0.2, "max": 0.7}
    assert p["summary"] == {"avg": 0.585, "min": 0.2, "max": 0.8, "count": 2}
    assert len(res.citations) >= 1
    assert box["url"] == (
        "http://vegetation.invalid/api/vegetation/scenes/"
        "urn%3Angsi-ld%3AAgriParcel%3Ap1/stats"
    )
    assert box["params"] == {"index_type": "NDVI", "months": 6}
    h = box["headers"]
    assert h["X-Tenant-ID"] == "acme" and h["X-User-ID"] == "u"
    assert h["X-User-Roles"] == "Farmer" and h["X-Auth-Signature"]


@pytest.mark.asyncio
async def test_summary_recomputed_from_kept_points(monkeypatch):
    _install(monkeypatch, {"data_points": _pts(45), "summary": {"count": 45, "avg": 9}})
    res = await vegetation.get_vegetation_trend(
        _ctx(), vegetation.GetVegetationTrendArgs(parcel_id=URN)
    )
    assert res.payload["summary"] == {"avg": 0.5, "min": 0.1, "max": 0.9, "count": 30}


@pytest.mark.asyncio
async def test_caps_to_newest_30(monkeypatch):
    _install(monkeypatch, {"data_points": _pts(45), "summary": {"count": 45}})
    res = await vegetation.get_vegetation_trend(
        _ctx(), vegetation.GetVegetationTrendArgs(parcel_id=URN)
    )
    pts = res.payload["points"]
    assert len(pts) == 30
    assert pts[-1]["date"] == res.payload["latest_date"] == _pts(45)[-1]["date"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"data_points": [], "summary": {"count": 0}}, 200),
    ({"data_points": [{"date": "2026-01-01", "mean": None}]}, 200),
    ({"detail": "No scenes"}, 404),
])
async def test_no_scenes_is_ok_empty_with_note(monkeypatch, body, status):
    _install(monkeypatch, body, status)
    res = await vegetation.get_vegetation_trend(
        _ctx(), vegetation.GetVegetationTrendArgs(parcel_id=URN)
    )
    assert res.ok is True
    assert res.payload["points"] == [] and res.payload["note"]
    assert len(res.citations) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"detail": "x"}, 500), (["x"], 200), ({"data_points": "no"}, 200),
    ({"data_points": [{"mean": 0.1}]}, 200), ({}, 200), (ValueError("bad"), 200),
])
async def test_failures_are_fail_closed(monkeypatch, body, status):
    _install(monkeypatch, body, status)
    res = await vegetation.get_vegetation_trend(
        _ctx(), vegetation.GetVegetationTrendArgs(parcel_id=URN)
    )
    assert res.ok is False and res.unavailable_reason == "vegetation scenes unreachable"


@pytest.mark.asyncio
async def test_unconfigured_does_not_call(monkeypatch):
    box = _install(monkeypatch, {}, url="")
    res = await vegetation.get_vegetation_trend(
        _ctx(), vegetation.GetVegetationTrendArgs(parcel_id=URN)
    )
    assert res.ok is False and box["calls"] == 0


def test_registration():
    t = {t.name: t for t in available_tools()}["get_vegetation_trend"]
    assert t.required_module == "vegetation-prime"
    assert t.required_setting == "vegetation_url"
    assert t.timeout_s == 12.0
    assert t.description == (
        "NDVI of one parcel over the last six months from satellite scenes, "
        "with the date of the latest scene."
    )
