"""get_risk_catalog: risk module catalog of alert types."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.domain.session import SessionContext
from app.tools.adapters import risk
from app.tools.catalog import available_tools


def _ctx() -> SessionContext:
    return SessionContext("acme", "u", ("Farmer", "TenantAdmin"), "mobile", "1", "t")


class _Response:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise httpx.HTTPStatusError(
                "e", request=httpx.Request("GET", "http://x"), response=httpx.Response(self.status)
            )

    def json(self):
        return self._body


def _install(monkeypatch, body, status=200, url="http://risk.invalid"):
    box = {"calls": 0}

    class _Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, u, headers=None, params=None):
            box.update(url=u, headers=headers)
            box["calls"] += 1
            return _Response(body, status)

    monkeypatch.setattr(risk.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        risk, "get_settings", lambda: SimpleNamespace(risk_url=url, hmac_secret="s")
    )
    return box


@pytest.mark.asyncio
async def test_maps_rows_and_empty_groups_stay_list(monkeypatch):
    box = _install(
        monkeypatch,
        [
            {"alert_type": "frost", "name": "n", "category": "weather",
             "applicable_crop_groups": ["cereal"], "documentation": "x"},
            {"alert_type": "water", "category": "agronomic", "applicable_crop_groups": []},
            {"alert_type": "pest", "category": "agronomic", "applicable_crop_groups": None},
        ],
    )
    res = await risk.get_risk_catalog(_ctx(), risk.GetRiskCatalogArgs())
    assert res.ok is True
    assert box["url"] == "http://risk.invalid/api/risk/catalog"
    assert box["headers"]["X-User-Roles"] == "Farmer,TenantAdmin"
    assert {"X-Tenant-ID", "X-User-ID", "X-Auth-Signature"} <= set(box["headers"])
    assert res.payload == [
        {"alertType": "frost", "category": "weather", "applicable_crop_groups": ["cereal"]},
        {"alertType": "water", "category": "agronomic", "applicable_crop_groups": []},
        {"alertType": "pest", "category": "agronomic", "applicable_crop_groups": []},
    ]
    assert len(res.citations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [({"x": 1}, 200), ([], 500)])
async def test_failures_are_unavailable(monkeypatch, body, status):
    _install(monkeypatch, body, status)
    res = await risk.get_risk_catalog(_ctx(), risk.GetRiskCatalogArgs())
    assert res.ok is False
    assert res.unavailable_reason == "risk catalog unreachable"
    assert res.citations == ()


@pytest.mark.asyncio
async def test_empty_url_no_http(monkeypatch):
    box = _install(monkeypatch, [], url="")
    res = await risk.get_risk_catalog(_ctx(), risk.GetRiskCatalogArgs())
    assert res.ok is False and box["calls"] == 0


def test_registered():
    t = {t.name: t for t in available_tools()}["get_risk_catalog"]
    assert (t.required_module, t.required_setting) == ("risk", "risk_url")
