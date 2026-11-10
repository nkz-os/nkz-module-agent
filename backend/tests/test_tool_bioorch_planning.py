"""get_cereal_milestones / get_crop_fit: bioorchestrator planning routes."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.domain.session import SessionContext
from app.tools.adapters import bioorch
from app.tools.catalog import available_tools

URN = "urn:ngsi-ld:AgriParcel:p1"


def _ctx() -> SessionContext:
    return SessionContext("acme", "u", ("Farmer",), "mobile", "1", "t")


class _Response:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise httpx.HTTPStatusError(
                "e",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status),
            )

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _install(monkeypatch, body, status=200, secret="sek"):
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

    monkeypatch.setattr(bioorch.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        bioorch,
        "get_settings",
        lambda: SimpleNamespace(
            bioorch_api_url="http://bioorch.invalid", internal_service_secret=secret
        ),
    )
    return box


PHENO = {
    "state": "done",
    "unavailable": [],
    "extra": 1,
    "season": {
        "crop": "TRZAX",
        "sowing": {"date": "2026-11-02", "source": "crop_cycle", "provenance": "actual"},
        "regime": "rainfed",
        "as_of": "2026-11-09",
    },
    "phenology": {
        "milestones": {
            "emergence": {"gdd": 100.0, "status": "projected", "date_p10": "2026-11-20",
                          "date_p50": "2026-11-25", "date_p90": "2026-12-03"},
            "maturity": {"gdd": 2000.0, "status": "unavailable", "reason": "not_reached"},
        }
    },
}


@pytest.mark.asyncio
async def test_milestones_happy_and_headers(monkeypatch):
    box = _install(monkeypatch, PHENO)
    res = await bioorch.get_cereal_milestones(
        _ctx(), bioorch.GetCerealMilestonesArgs(parcel_id=URN)
    )
    assert res.ok is True
    p = res.payload
    assert p["state"] == "done" and p["crop"] == "TRZAX"
    assert p["sowing"] == {"date": "2026-11-02", "provenance": "actual"}
    assert p["regime"] == "rainfed" and p["as_of"] == "2026-11-09"
    assert p["milestones"]["emergence"]["date_p50"] == "2026-11-25"
    assert p["milestones"]["maturity"]["reason"] == "not_reached"
    assert "extra" not in p
    assert len(res.citations) >= 1
    assert box["url"] == (
        "http://bioorch.invalid/api/graph/planning/parcels/"
        "urn%3Angsi-ld%3AAgriParcel%3Ap1/phenology"
    )
    assert box["headers"] == {"X-Internal-Service-Secret": "sek", "X-Tenant-ID": "acme"}
    assert "X-Auth-Signature" not in box["headers"]


@pytest.mark.asyncio
async def test_milestones_unsupported_crop_is_ok(monkeypatch):
    _install(monkeypatch, {
        "state": "unavailable", "season": None, "phenology": None,
        "unavailable": [{"provider_id": "aquacrop_calendar", "reason": "unsupported_crop"}],
    })
    res = await bioorch.get_cereal_milestones(
        _ctx(), bioorch.GetCerealMilestonesArgs(parcel_id=URN)
    )
    assert res.ok is True
    assert res.payload == {"state": "unavailable", "unavailable_reason": "unsupported_crop"}
    assert len(res.citations) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [({"detail": "x"}, 503), ({"detail": "x"}, 404),
                                         (["x"], 200),
                                         (ValueError("bad json"), 200),
                                         ({"state": "failed"}, 200)])
async def test_milestones_failures_are_fail_closed(monkeypatch, body, status):
    _install(monkeypatch, body, status)
    res = await bioorch.get_cereal_milestones(
        _ctx(), bioorch.GetCerealMilestonesArgs(parcel_id=URN)
    )
    assert res.ok is False
    assert res.unavailable_reason == "bioorchestrator unreachable"


@pytest.mark.asyncio
async def test_milestones_unknown_state_is_ok_unavailable(monkeypatch):
    _install(monkeypatch, {"state": "queued", "unavailable": []})
    res = await bioorch.get_cereal_milestones(
        _ctx(), bioorch.GetCerealMilestonesArgs(parcel_id=URN)
    )
    assert res.ok is True
    assert res.payload == {"state": "unavailable", "unavailable_reason": "unknown_state"}
    assert len(res.citations) >= 1


@pytest.mark.asyncio
async def test_empty_secret_does_not_call(monkeypatch):
    box = _install(monkeypatch, PHENO, secret="")
    for coro in (
        bioorch.get_cereal_milestones(_ctx(), bioorch.GetCerealMilestonesArgs(parcel_id=URN)),
        bioorch.get_crop_fit(_ctx(), bioorch.GetCropFitArgs(parcel_id=URN)),
    ):
        res = await coro
        assert res.ok is False
        assert res.unavailable_reason == "bioorchestrator unreachable"
    assert box["calls"] == 0


def _fit(n, verdicts=None):
    verdicts = verdicts or ["no_fit"] * n
    return {
        "parcel_id": URN, "campaign": 2026, "graphs": [],
        "species": [
            {"species": f"s{i}", "eppo": f"E{i}", "names": {"es": "x"}, "verdict": v,
             "reasons": [{"code": "ph_low", "gate": "soil", "params": {"a": 1}}],
             "sowing": {"sowing_type": "autumn", "start_month": 10, "end_month": 11},
             "irrigation_need": {"percentiles": {"p10": 1.0, "p50": 2.0, "p90": 3.0},
                                 "unit": "mm", "n_years": 5},
             "has_variety_trials": True}
            for i, v in enumerate(verdicts)
        ],
    }


@pytest.mark.asyncio
async def test_fit_happy_orders_fits_first_and_caps(monkeypatch):
    box = _install(monkeypatch, _fit(20, ["no_fit"] * 10 + ["caveats"] * 5 + ["fits"] * 5))
    res = await bioorch.get_crop_fit(
        _ctx(), bioorch.GetCropFitArgs(parcel_id=URN, campaign="2026/27")
    )
    assert res.ok is True
    rows = res.payload["species"]
    assert len(rows) == 15
    assert [r["verdict"] for r in rows[:5]] == ["fits"] * 5
    assert rows[0]["reasons"] == [{"code": "ph_low", "gate": "soil"}]
    assert "names" not in rows[0] and "has_variety_trials" not in rows[0]
    assert rows[0]["irrigation_need"]["percentiles"]["p50"] == 2.0
    assert box["params"] == {"campaign": 2026}
    assert box["url"].endswith("/parcels/urn%3Angsi-ld%3AAgriParcel%3Ap1/fit")
    assert box["headers"]["X-Internal-Service-Secret"] == "sek"
    assert "X-Auth-Signature" not in box["headers"]
    assert len(res.citations) >= 1


@pytest.mark.asyncio
async def test_fit_default_campaign_and_bad_campaign(monkeypatch):
    box = _install(monkeypatch, _fit(1, ["fits"]))
    res = await bioorch.get_crop_fit(_ctx(), bioorch.GetCropFitArgs(parcel_id=URN))
    assert res.ok is True and isinstance(box["params"]["campaign"], int)
    with pytest.raises(ValidationError):
        bioorch.GetCropFitArgs(parcel_id=URN, campaign="next year")


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [({"detail": "x"}, 503), ({"species": "no"}, 200),
                                         ([], 200), (ValueError("bad"), 200)])
async def test_fit_failures_are_fail_closed(monkeypatch, body, status):
    _install(monkeypatch, body, status)
    res = await bioorch.get_crop_fit(_ctx(), bioorch.GetCropFitArgs(parcel_id=URN))
    assert res.ok is False
    assert res.unavailable_reason == "bioorchestrator unreachable"


def test_registration():
    tools = {t.name: t for t in available_tools()}
    for name in ("get_cereal_milestones", "get_crop_fit"):
        t = tools[name]
        assert t.required_module == "bioorchestrator"
        assert t.required_setting == "bioorch_api_url"
        assert t.timeout_s == 12.0
    assert tools["get_cereal_milestones"].description.startswith(
        "For wheat, durum wheat and barley:")
    assert tools["get_crop_fit"].description.startswith("Which crops fit one parcel")
