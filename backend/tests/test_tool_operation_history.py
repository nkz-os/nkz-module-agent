"""get_operation_history: field-operations list, sorted and truncated."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.domain.session import SessionContext
from app.tools.adapters import field_ops
from app.tools.catalog import PARCEL_NOT_FOUND, available_tools

PARCEL = "urn:ngsi-ld:AgriParcel:p1"


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


def _install(monkeypatch, body, status=200, url="http://fieldops.invalid"):
    box = {"calls": 0}

    class _Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, u, params=None, headers=None):
            box.update(url=u, params=params, headers=headers)
            box["calls"] += 1
            return _Response(body, status)

    monkeypatch.setattr(field_ops.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        field_ops, "get_settings", lambda: SimpleNamespace(field_ops_url=url, hmac_secret="s")
    )
    return box


async def _run(monkeypatch, rows, count=None, **args):
    body = {"operations": rows, "count": len(rows) if count is None else count}
    box = _install(monkeypatch, body)
    res = await field_ops.get_operation_history(
        _ctx(), field_ops.GetOperationHistoryArgs(parcel_id=PARCEL, **args)
    )
    return res, box


@pytest.mark.asyncio
async def test_request_shape_and_headers(monkeypatch):
    res, box = await _run(monkeypatch, [], operation_type="spraying")
    assert res.ok is True and res.payload == {"operations": [], "truncated": False}
    assert box["url"] == "http://fieldops.invalid/api/field-operations/operations"
    assert box["params"] == {"parcel_id": PARCEL, "limit": 100, "operation_type": "spraying"}
    assert box["headers"]["X-User-Roles"] == "Farmer,TenantAdmin"
    assert {"X-Tenant-ID", "X-User-ID", "X-Auth-Signature"} <= set(box["headers"])
    assert res.citations[0].source == "field-operations:operations"


@pytest.mark.asyncio
async def test_sorted_by_best_date_newest_first_and_limited(monkeypatch):
    rows = [
        {"id": "a", "operationType": "sowing", "endedAt": "2026-10-01T10:00:00Z"},
        {"id": "b", "operationType": "tillage", "startedAt": "2026-10-05T10:00:00Z"},
        {"id": "c", "operationType": "spraying", "plannedDate": "2026-10-03T10:00:00Z"},
        {"id": "d", "operationType": "scouting", "dateCreated": "2026-09-01T10:00:00Z"},
        {"id": "e", "operationType": "scouting"},
    ]
    res, _ = await _run(monkeypatch, rows)
    assert [o["id"] for o in res.payload["operations"]] == ["b", "c", "a", "d", "e"]
    assert res.payload["operations"][0]["date"] == "2026-10-05T10:00:00Z"
    res, _ = await _run(monkeypatch, rows, limit=2)
    assert [o["id"] for o in res.payload["operations"]] == ["b", "c"]


@pytest.mark.asyncio
async def test_truncated_flag(monkeypatch):
    rows = [{"id": "a", "operationType": "sowing"}]
    res, _ = await _run(monkeypatch, rows, count=100)
    assert res.payload["truncated"] is True
    res, _ = await _run(monkeypatch, rows, count=99)
    assert res.payload["truncated"] is False


@pytest.mark.asyncio
async def test_truncated_when_more_rows_than_limit(monkeypatch):
    rows = [
        {"id": f"r{i}", "operationType": "sowing", "date": f"2026-01-{i % 28 + 1:02d}"}
        for i in range(60)
    ]
    res, _ = await _run(monkeypatch, rows, limit=20)
    assert len(res.payload["operations"]) == 20
    assert res.payload["truncated"] is True


@pytest.mark.asyncio
async def test_product_rate_and_units(monkeypatch):
    rows = [
        {"id": "f", "operationType": "fertilization", "fertilizerType": "urea", "fertilizerRate": 120},
        {"id": "i", "operationType": "irrigation", "waterPerHectare": 300.5},
        {"id": "s", "operationType": "spraying", "productName": "X", "productRate": 1.5},
        {"id": "w", "operationType": "sowing", "variety": "V1", "seedingRate": 180},
        {"id": "h", "operationType": "harvesting", "harvestedWeight": 5000},
        {"id": "t", "operationType": "tillage", "tillageType": "disc"},
    ]
    res, _ = await _run(monkeypatch, rows)
    by = {o["id"]: o for o in res.payload["operations"]}
    assert (by["f"]["product"], by["f"]["rate"], by["f"]["rate_unit"]) == ("urea", 120.0, "kg/ha")
    assert (by["i"]["rate"], by["i"]["rate_unit"]) == (300.5, "L/ha")
    assert (by["s"]["product"], by["s"]["rate_unit"]) == ("X", "L/ha")
    assert (by["w"]["product"], by["w"]["rate_unit"]) == ("V1", "kg/ha")
    assert (by["h"]["rate"], by["h"]["rate_unit"]) == (5000.0, "kg")
    assert by["t"]["product"] == "disc" and "rate" not in by["t"]


@pytest.mark.asyncio
async def test_missing_operation_type_row_skipped(monkeypatch):
    rows = [{"id": "x"}, {"operationType": "sowing"}, "junk", {"id": "ok", "operationType": "sowing"}]
    res, _ = await _run(monkeypatch, rows)
    assert [o["id"] for o in res.payload["operations"]] == ["ok"]


@pytest.mark.asyncio
async def test_failures_and_empty_url(monkeypatch):
    for body, status in (({"operations": []}, 500), ({"nope": 1}, 200), ([], 200)):
        _install(monkeypatch, body, status)
        res = await field_ops.get_operation_history(
            _ctx(), field_ops.GetOperationHistoryArgs(parcel_id=PARCEL)
        )
        assert res.ok is False and res.unavailable_reason == "operation history unreachable"
    _install(monkeypatch, {"detail": "x"}, 404)
    res = await field_ops.get_operation_history(
        _ctx(), field_ops.GetOperationHistoryArgs(parcel_id=PARCEL)
    )
    assert res.ok is False and res.unavailable_reason == PARCEL_NOT_FOUND
    box = _install(monkeypatch, {"operations": []}, url="")
    res = await field_ops.get_operation_history(
        _ctx(), field_ops.GetOperationHistoryArgs(parcel_id=PARCEL)
    )
    assert res.ok is False and box["calls"] == 0


def test_args_validation_and_registration():
    for bad in ({"limit": 0}, {"limit": 51}, {"operation_type": "mowing"}):
        with pytest.raises(ValidationError):
            field_ops.GetOperationHistoryArgs(parcel_id=PARCEL, **bad)
    t = {t.name: t for t in available_tools()}["get_operation_history"]
    assert (t.required_module, t.required_setting) == ("field-operations", "field_ops_url")
