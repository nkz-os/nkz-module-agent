"""get_vigour_zones: crop-health intra-parcel zones."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.domain.session import SessionContext
from app.tools.adapters import crop_health
from app.tools.catalog import available_tools

URN = "urn:ngsi-ld:AgriParcel:p1"


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


def _zone(zid, rank, differs=True, **kw):
    return {
        "zoneId": zid, "zoneRank": rank, "vigorIndex": 0.4, "vigorCondition": "LOW",
        "cwsiValue": 0.5, "compositeStressIndex": 60.0, "overallSeverity": "HIGH",
        "wetnessClass": "dry", "inputFidelity": {"vigor": "zone"},
        "geometry": {"type": "Polygon", "coordinates": []}, "zoneUrn": "u",
        "differsFromParcel": differs, **kw,
    }


@pytest.mark.asyncio
async def test_happy_sorted_by_rank_geometry_dropped_headers(monkeypatch):
    box = _install(monkeypatch, {
        "isWholeParcel": False,
        "parcelValues": {"cwsiValue": 0.2, "compositeStressIndex": 20.0,
                         "vigorIndex": 0.7, "overallSeverity": "LOW", "junk": 1},
        "zones": [_zone("z3", 3), _zone("z1", 1), _zone("zx", 2, differs=False),
                  _zone("z2", None)],
    })
    res = await crop_health.get_vigour_zones(
        _ctx(), crop_health.GetVigourZonesArgs(parcel_id=URN)
    )
    assert res.ok is True
    p = res.payload
    assert p["isWholeParcel"] is False
    assert [z["zoneId"] for z in p["zones"]] == ["z1", "z3", "z2"]
    assert p["parcel"] == {"cwsiValue": 0.2, "compositeStressIndex": 20.0,
                           "vigorIndex": 0.7, "overallSeverity": "LOW"}
    z = p["zones"][0]
    assert "geometry" not in z and "zoneUrn" not in z and "differsFromParcel" not in z
    assert z["wetnessClass"] == "dry" and z["inputFidelity"] == {"vigor": "zone"}
    assert len(res.citations) >= 1
    assert box["params"] == {"parcelId": "p1"}
    assert box["url"] == "http://crop-health.invalid/api/crop-health/assessments/zones"
    h = box["headers"]
    assert h["X-Tenant-ID"] == "acme" and h["X-User-ID"] == "u"
    assert h["X-User-Roles"] == "Farmer,TenantAdmin" and h["X-Auth-Signature"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"zones": [], "isWholeParcel": True, "parcelValues": {}}, 200),
    ({"detail": "No zoning"}, 404),
    ({"zones": [_zone("z1", 1, differs=False)], "parcelValues": {}}, 200),
])
async def test_no_zoning_is_ok_empty_with_note(monkeypatch, body, status):
    _install(monkeypatch, body, status)
    res = await crop_health.get_vigour_zones(
        _ctx(), crop_health.GetVigourZonesArgs(parcel_id=URN)
    )
    assert res.ok is True
    assert res.payload["zones"] == [] and res.payload["note"]
    assert len(res.citations) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"detail": "x"}, 503), (["x"], 200), ({"zones": "no"}, 200),
    ({"zones": [{"zoneRank": 1}]}, 200), (ValueError("bad"), 200),
])
async def test_failures_are_fail_closed(monkeypatch, body, status):
    _install(monkeypatch, body, status)
    res = await crop_health.get_vigour_zones(
        _ctx(), crop_health.GetVigourZonesArgs(parcel_id=URN)
    )
    assert res.ok is False and res.unavailable_reason == "vigour zones unreachable"


@pytest.mark.asyncio
async def test_upstream_whole_parcel_flag_wins(monkeypatch):
    _install(monkeypatch, {"isWholeParcel": True, "zones": [_zone("z1", 1)],
                           "parcelValues": {}})
    res = await crop_health.get_vigour_zones(
        _ctx(), crop_health.GetVigourZonesArgs(parcel_id=URN)
    )
    assert res.ok is True and res.payload["isWholeParcel"] is True
    assert res.payload["zones"] == []


@pytest.mark.asyncio
async def test_unconfigured_does_not_call(monkeypatch):
    box = _install(monkeypatch, {}, url="")
    res = await crop_health.get_vigour_zones(
        _ctx(), crop_health.GetVigourZonesArgs(parcel_id=URN)
    )
    assert res.ok is False and box["calls"] == 0


def test_registration():
    t = {t.name: t for t in available_tools()}["get_vigour_zones"]
    assert t.required_module == "crop-health"
    assert t.required_setting == "crop_health_url"
    assert t.timeout_s == 12.0
    assert t.description == (
        "Intra-parcel zones whose vigour or stress differs from the parcel "
        "average, ranked worst first."
    )
