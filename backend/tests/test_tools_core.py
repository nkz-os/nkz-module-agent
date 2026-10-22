"""Core read-only tools: schema validation, tenant isolation, signed calls.

The tools are adapters: they are blind to tenant and only read ``ctx``. These
tests attack that property -- an adapter that hardcodes or caches a tenant must
go red because another tenant's data leaks into the result, not because it
throws.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from nkz_platform_sdk.crypto import verify_hmac_signature
from pydantic import ValidationError

from app.domain.session import SessionContext
from app.tools.adapters import orion, timeseries


def _ctx(tenant: str = "tenant_a") -> SessionContext:
    return SessionContext(
        tenant_id=tenant,
        user_id="user_a",
        roles=("Farmer",),
        channel="telegram",
        channel_user_id="42",
        trace_id="t-1",
    )


def _settings(url: str = "http://timeseries.invalid", secret: str = "test-secret"):
    return SimpleNamespace(timeseries_url=url, hmac_secret=secret)


# --- Schemas: out-of-range and injection inputs must be rejected at the edge. ---


def test_days_above_90_rejected():
    with pytest.raises(ValidationError):
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:p1",
            metric=timeseries.Metric.soil_moisture,
            days=91,
        )


def test_days_below_1_rejected():
    with pytest.raises(ValidationError):
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:p1",
            metric=timeseries.Metric.soil_moisture,
            days=0,
        )


def test_invalid_metric_rejected():
    with pytest.raises(ValidationError):
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:p1",
            metric="foo",
            days=7,
        )


def test_parcel_id_injection_rejected_in_snapshot():
    with pytest.raises(ValidationError):
        orion.GetParcelSnapshotArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:x?type=AgriParcel"
        )


def test_parcel_id_injection_rejected_in_timeseries():
    with pytest.raises(ValidationError):
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:x?type=AgriParcel",
            metric=timeseries.Metric.ndvi,
            days=7,
        )


@pytest.mark.parametrize(
    "bad",
    [
        "..", ".", "urn:ngsi-ld:..:x", "urn:ngsi-ld:AgriParcel:.", "urn:ngsi-ld:a:..",
        "../data", "AgriParcel:p1", "p1", "urn:ngsi-ld:", "urn:ngsi-ld:a/b",
        "urn:ngsi-ld:a%2e%2e", "urn:ngsi-ld:" + "x" * 121,
    ],
)
def test_parcel_id_must_be_an_urn_without_dot_segments(bad):
    with pytest.raises(ValidationError):
        orion.GetParcelSnapshotArgs(parcel_id=bad)
    with pytest.raises(ValidationError):
        timeseries.GetTimeseriesArgs(
            parcel_id=bad, metric=timeseries.Metric.ndvi, days=7
        )


@pytest.mark.parametrize(
    "good", ["urn:ngsi-ld:AgriParcel:a1", "urn:ngsi-ld:AgriParcel:t.1:p-2_x", "urn:ngsi-ld:x" + "y" * 100]
)
def test_parcel_id_accepts_real_urns(good):
    assert orion.GetParcelSnapshotArgs(parcel_id=good).parcel_id == good


# --- Tenant isolation: the most important property. ---

def _polygon(lon: float, lat: float) -> dict:
    d = 0.01
    ring = [
        [lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat],
    ]
    return {"type": "GeoProperty", "value": {"type": "Polygon", "coordinates": [ring]}}


def _full_parcel(pid: str, name: str, area: float, lon: float, lat: float) -> dict:
    """Shape sampled from real parcels: typed DateTime values, Relationship
    objects as URNs, polygon GeoProperty."""
    return {
        "id": pid,
        "type": "AgriParcel",
        "name": {"type": "Property", "value": name},
        "category": {"type": "Property", "value": "cadastral"},
        "area": {"type": "Property", "value": area},
        "irrigationRegime": {"type": "Property", "value": "rainfed"},
        "cropSeasonStart": {
            "type": "Property",
            "value": {"@type": "DateTime", "@value": "2026-03-01T00:00:00Z"},
        },
        "cropSeasonEnd": {
            "type": "Property",
            "value": {"@type": "DateTime", "@value": "2026-07-15T00:00:00Z"},
        },
        "hasAgriCrop": {"type": "Relationship", "object": "urn:ngsi-ld:AgriCrop:wheat"},
        "hasAgriCropVariety": {
            "type": "Relationship", "object": "urn:ngsi-ld:AgriCropVariety:v1",
        },
        "lastPlantedAt": {"type": "Property", "value": "2026-03-02"},
        "elevation": {"type": "Property", "value": 640.5},
        "terrainSlope": {"type": "Property", "value": 3.2},
        "terrainAspect": {"type": "Property", "value": 180.0},
        "timeZone": {"type": "Property", "value": "Europe/Madrid"},
        "ndviEnabled": {"type": "Property", "value": True},
        "location": _polygon(lon, lat),
    }


def _bare_parcel(pid: str, name: str) -> dict:
    """Only what every writer path sets: no crop, no optional attributes."""
    return {
        "id": pid,
        "type": "AgriParcel",
        "name": {"type": "Property", "value": name},
        "category": {"type": "Property", "value": "cadastral"},
        "area": {"type": "Property", "value": 5.0},
        "location": _polygon(2.0, 2.0),
    }


ENTITIES_A = [
    _full_parcel("urn:ngsi-ld:AgriParcel:a1", "A1", 10.0, 0.0, 0.0),
    _bare_parcel("urn:ngsi-ld:AgriParcel:a2", "A2"),
]

ENTITIES_B = [_bare_parcel("urn:ngsi-ld:AgriParcel:b1", "B1")]


class _TenantAwareOrionClient:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    async def query_entities(self, type=None, limit=100, **kwargs):
        assert type == "AgriParcel"
        assert limit == 50
        return ENTITIES_A if self.tenant_id == "tenant_a" else ENTITIES_B

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_get_parcels_isolates_tenant_data(monkeypatch):
    monkeypatch.setattr(orion, "OrionClient", _TenantAwareOrionClient)

    a = await orion.get_parcels(_ctx("tenant_a"), orion.GetParcelsArgs())
    b = await orion.get_parcels(_ctx("tenant_b"), orion.GetParcelsArgs())

    ids_a = {p["id"] for p in a.payload}
    ids_b = {p["id"] for p in b.payload}
    a_ids = {"urn:ngsi-ld:AgriParcel:a1", "urn:ngsi-ld:AgriParcel:a2"}
    b_ids = {"urn:ngsi-ld:AgriParcel:b1"}

    assert ids_a == a_ids
    assert ids_b == b_ids
    # The property itself: tenant A's result carries zero tenant B ids, and
    # vice versa. A hardcoded/cached client fails here by leaking ids, not by
    # raising.
    assert not (ids_a & b_ids)
    assert not (ids_b & a_ids)


# --- Parcel payloads: frozen surface from the real writer. ---


class _EntityOrionClient:
    entity: ClassVar[dict] = {}

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    async def get_entity(self, entity_id: str, options=None):
        return self.entity

    async def query_entities(self, type=None, limit=100, **kwargs):
        return [self.entity]

    async def close(self):
        pass


def _client_for(entity: dict):
    return type("_C", (_EntityOrionClient,), {"entity": entity})


@pytest.mark.asyncio
async def test_get_parcel_snapshot_freezes_surface(monkeypatch):
    entity = _full_parcel("urn:ngsi-ld:AgriParcel:a1", "A1", 10.0, 0.0, 0.0)
    entity["evil"] = {"type": "Property", "value": "secret"}
    monkeypatch.setattr(orion, "OrionClient", _client_for(entity))

    result = await orion.get_parcel_snapshot(
        _ctx(), orion.GetParcelSnapshotArgs(parcel_id="urn:ngsi-ld:AgriParcel:a1")
    )

    assert result.ok is True
    p = result.payload
    assert p["name"] == "A1"
    assert p["crop"] == "urn:ngsi-ld:AgriCrop:wheat"
    assert p["cropVariety"] == "urn:ngsi-ld:AgriCropVariety:v1"
    assert p["cropSeasonStart"] == "2026-03-01T00:00:00Z"
    assert p["irrigationRegime"] == "rainfed"
    assert p["elevation"] == 640.5
    assert p["ndviEnabled"] is True
    assert p["timeZone"] == "Europe/Madrid"
    # Centroid only, rounded; the polygon never reaches the model.
    assert p["centroid"] == {"lon": 0.005, "lat": 0.005}
    assert "location" not in p
    assert "evil" not in p
    assert "coordinates" not in str(p)


@pytest.mark.asyncio
async def test_parcel_reads_legacy_crop_relationship(monkeypatch):
    entity = _bare_parcel("urn:ngsi-ld:AgriParcel:a2", "A2")
    entity["refAgriCrop"] = {"type": "Relationship", "object": "urn:ngsi-ld:AgriCrop:barley"}
    monkeypatch.setattr(orion, "OrionClient", _client_for(entity))
    result = await orion.get_parcels(_ctx(), orion.GetParcelsArgs())
    assert result.payload[0]["crop"] == "urn:ngsi-ld:AgriCrop:barley"


@pytest.mark.asyncio
async def test_parcel_without_optional_attributes_is_compact(monkeypatch):
    monkeypatch.setattr(
        orion, "OrionClient", _client_for(_bare_parcel("urn:ngsi-ld:AgriParcel:a2", "A2"))
    )
    result = await orion.get_parcels(_ctx(), orion.GetParcelsArgs())
    assert result.payload == [
        {"id": "urn:ngsi-ld:AgriParcel:a2", "name": "A2", "category": "cadastral", "area": 5.0}
    ]


@pytest.mark.asyncio
async def test_get_parcels_list_has_no_geometry(monkeypatch):
    monkeypatch.setattr(orion, "OrionClient", _TenantAwareOrionClient)
    result = await orion.get_parcels(_ctx("tenant_a"), orion.GetParcelsArgs())
    assert "coordinates" not in str(result.payload)
    assert all("location" not in p and "centroid" not in p for p in result.payload)


# --- Timeseries: signed, tenant-scoped request. ---


class _RecordingResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {
            "entity_type": "DeviceMeasurement",
            "parcel_id": "urn:ngsi-ld:AgriParcel:p1",
            "count": 1,
            "data": [
                {
                    "entity_id": "urn:ngsi-ld:DeviceMeasurement:p1-1",
                    "observed_at": "2026-01-01T00:00:00+00:00",
                    "soil_moisture": 0.3,
                }
            ],
        }


class _RecordingClient:
    def __init__(self, timeout=None):
        self.captured = {}
        self.captured["timeout"] = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        self.captured["url"] = url
        self.captured["params"] = params
        self.captured["headers"] = headers
        return _RecordingResponse()


@pytest.mark.asyncio
async def test_get_timeseries_sends_signed_tenant_scoped_request(monkeypatch):
    created = {}

    def factory(timeout=None):
        client = _RecordingClient(timeout=timeout)
        created["client"] = client
        return client

    monkeypatch.setattr(timeseries.httpx, "AsyncClient", factory)
    monkeypatch.setattr(timeseries, "get_settings", lambda: _settings())

    result = await timeseries.get_timeseries(
        _ctx("tenant_x"),
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:p1",
            metric=timeseries.Metric.soil_moisture,
            days=7,
        ),
    )

    client = created["client"]
    assert result.ok is True
    assert client.captured["timeout"] == 8.0
    assert client.captured["headers"]["X-Tenant-ID"] == "tenant_x"

    signature = client.captured["headers"]["X-Auth-Signature"]
    assert signature
    assert verify_hmac_signature("test-secret", signature, "", "tenant_x")

    assert client.captured["params"]["attrs"] == "soil_moisture"
    assert "urn:ngsi-ld:AgriParcel:p1" in client.captured["url"]
    assert "/type/DeviceMeasurement/parcel/" in client.captured["url"]

    from_iso = datetime.fromisoformat(client.captured["params"]["from"])
    to_iso = datetime.fromisoformat(client.captured["params"]["to"])
    assert to_iso - from_iso == timedelta(days=7)


@pytest.mark.asyncio
async def test_get_timeseries_uses_eoproduct_for_ndvi(monkeypatch):
    created = {}

    def factory(timeout=None):
        client = _RecordingClient(timeout=timeout)
        created["client"] = client
        return client

    monkeypatch.setattr(timeseries.httpx, "AsyncClient", factory)
    monkeypatch.setattr(timeseries, "get_settings", lambda: _settings())

    await timeseries.get_timeseries(
        _ctx(),
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:p1",
            metric=timeseries.Metric.ndvi,
            days=30,
        ),
    )

    client = created["client"]
    assert "/type/EOProduct/parcel/" in client.captured["url"]
    assert client.captured["params"]["attrs"] == "ndvi"


# --- Timeseries failures: ok=False, payload None, no exception escapes. ---


class _HttpErrorResponse:
    def raise_for_status(self):
        raise httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("GET", "http://timeseries.invalid"),
            response=httpx.Response(
                500, request=httpx.Request("GET", "http://timeseries.invalid")
            ),
        )


class _HtmlResponse:
    def raise_for_status(self):
        pass

    def json(self):
        raise ValueError("not json")


class _FailingClient:
    def __init__(self, timeout=None, response=None):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        return self._response


@pytest.mark.asyncio
async def test_get_timeseries_http_500_returns_unavailable(monkeypatch):
    monkeypatch.setattr(
        timeseries.httpx,
        "AsyncClient",
        lambda timeout=None: _FailingClient(response=_HttpErrorResponse()),
    )
    monkeypatch.setattr(timeseries, "get_settings", lambda: _settings())

    result = await timeseries.get_timeseries(
        _ctx(),
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:p1",
            metric=timeseries.Metric.ndvi,
            days=30,
        ),
    )

    assert result.ok is False
    assert result.unavailable_reason == "timeseries unreachable"
    assert result.payload is None


@pytest.mark.asyncio
async def test_get_timeseries_html_body_returns_unavailable(monkeypatch):
    monkeypatch.setattr(
        timeseries.httpx,
        "AsyncClient",
        lambda timeout=None: _FailingClient(response=_HtmlResponse()),
    )
    monkeypatch.setattr(timeseries, "get_settings", lambda: _settings())

    result = await timeseries.get_timeseries(
        _ctx(),
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:p1",
            metric=timeseries.Metric.soil_moisture,
            days=7,
        ),
    )

    assert result.ok is False
    assert result.unavailable_reason == "timeseries unreachable"
    assert result.payload is None


# --- Citations on successful results. ---


class _ListOrionClient:
    def __init__(self, tenant_id: str):
        pass

    async def query_entities(self, type=None, limit=100, **kwargs):
        return ENTITIES_A

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_get_parcels_result_carries_citation(monkeypatch):
    monkeypatch.setattr(orion, "OrionClient", _ListOrionClient)

    result = await orion.get_parcels(_ctx(), orion.GetParcelsArgs())

    assert result.ok is True
    assert len(result.citations) == 1
    assert result.citations[0].source == "orion:AgriParcel"


@pytest.mark.asyncio
async def test_get_timeseries_result_carries_citation(monkeypatch):
    created = {}

    def factory(timeout=None):
        client = _RecordingClient(timeout=timeout)
        created["client"] = client
        return client

    monkeypatch.setattr(timeseries.httpx, "AsyncClient", factory)
    monkeypatch.setattr(timeseries, "get_settings", lambda: _settings())

    result = await timeseries.get_timeseries(
        _ctx(),
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:p1",
            metric=timeseries.Metric.soil_moisture,
            days=7,
        ),
    )

    assert result.ok is True
    assert len(result.citations) == 1
    assert result.citations[0].source == "timeseries-reader"


# --- Fail-safe paths: an adapter never raises to the turn. ---


class _ExplodingOrionClient:
    """Fake whose every call raises, aligned to the real OrionClient surface."""

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    async def query_entities(self, **_):
        raise RuntimeError("orion exploded")

    async def get_entity(self, *_args, **_kwargs):
        raise RuntimeError("orion exploded")

    async def close(self):
        pass


class _NoneEntityOrionClient:
    """Fake whose get_entity returns None (URN simply not there)."""

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    async def get_entity(self, *_args, **_kwargs):
        return None

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_get_parcels_orion_error_returns_unavailable(monkeypatch):
    monkeypatch.setattr(orion, "OrionClient", _ExplodingOrionClient)
    result = await orion.get_parcels(_ctx(), orion.GetParcelsArgs())
    assert result.ok is False
    assert result.payload is None
    assert result.unavailable_reason == "parcel list unavailable"


@pytest.mark.asyncio
async def test_get_parcel_snapshot_orion_error_returns_unavailable(monkeypatch):
    monkeypatch.setattr(orion, "OrionClient", _ExplodingOrionClient)
    result = await orion.get_parcel_snapshot(
        _ctx(), orion.GetParcelSnapshotArgs(parcel_id="urn:ngsi-ld:AgriParcel:x:1")
    )
    assert result.ok is False
    assert result.payload is None
    assert result.unavailable_reason == "parcel lookup unavailable"


@pytest.mark.asyncio
async def test_get_parcel_snapshot_missing_entity_returns_not_found(monkeypatch):
    monkeypatch.setattr(orion, "OrionClient", _NoneEntityOrionClient)
    result = await orion.get_parcel_snapshot(
        _ctx(), orion.GetParcelSnapshotArgs(parcel_id="urn:ngsi-ld:AgriParcel:x:1")
    )
    assert result.ok is False
    assert result.unavailable_reason == "parcel not found"


@pytest.mark.asyncio
async def test_get_timeseries_without_url_returns_unavailable(monkeypatch):
    monkeypatch.setattr(timeseries, "get_settings", lambda: _settings(url=""))
    result = await timeseries.get_timeseries(
        _ctx(),
        timeseries.GetTimeseriesArgs(parcel_id="urn:ngsi-ld:AgriParcel:x:1", metric=timeseries.Metric.precipitation),
    )
    assert result.ok is False
    assert result.unavailable_reason == "timeseries unreachable"


@pytest.mark.asyncio
async def test_get_timeseries_non_dict_body_returns_unavailable(monkeypatch):
    class _Settings:
        timeseries_url = "http://timeseries.invalid"
        hmac_secret = "test-secret"

    monkeypatch.setattr(timeseries, "get_settings", lambda: _Settings())

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='["not", "a", "dict"]')

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        timeseries.httpx, "AsyncClient", lambda **kw: orig(transport=transport, **kw)
    )
    result = await timeseries.get_timeseries(
        _ctx(),
        timeseries.GetTimeseriesArgs(parcel_id="urn:ngsi-ld:AgriParcel:x:1", metric=timeseries.Metric.precipitation),
    )
    assert result.ok is False
    assert result.payload is None
    assert result.unavailable_reason == "timeseries unreachable"


# --- Timeseries payload: newest N + summary, never the raw body. ---


def _series_body(n: int, newest_first: bool = True) -> dict:
    rows = [
        {
            "entity_id": f"urn:ngsi-ld:DeviceMeasurement:p1-{i}",
            "observed_at": f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00+00:00",
            "soil_moisture": float(i),
        }
        for i in range(n)
    ]
    return {"entity_type": "DeviceMeasurement", "count": n,
            "data": rows[::-1] if newest_first else rows}


async def _run_series(monkeypatch, body):
    class _Resp(_RecordingResponse):
        def json(self):
            return body

    class _Client(_RecordingClient):
        async def get(self, url, params=None, headers=None):
            return _Resp()

    monkeypatch.setattr(timeseries.httpx, "AsyncClient", lambda timeout=None: _Client(timeout))
    monkeypatch.setattr(timeseries, "get_settings", lambda: _settings())
    return await timeseries.get_timeseries(
        _ctx(),
        timeseries.GetTimeseriesArgs(
            parcel_id="urn:ngsi-ld:AgriParcel:p1",
            metric=timeseries.Metric.soil_moisture,
            days=7,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("newest_first", [True, False])
async def test_timeseries_keeps_the_newest_points_whatever_the_order(monkeypatch, newest_first):
    result = await _run_series(monkeypatch, _series_body(200, newest_first))
    p = result.payload
    assert result.ok is True
    assert p["total_points"] == 200
    assert len(p["points"]) == 50
    # Newest first, and the newest value (199.0) is in, the oldest is out.
    assert p["points"][0]["value"] == 199.0
    assert p["points"][-1]["value"] == 150.0
    s = p["summary"]
    assert s["count"] == 200
    assert s["min"]["value"] == 0.0 and s["max"]["value"] == 199.0
    assert s["last"]["value"] == 199.0
    assert s["mean"] == pytest.approx(99.5)
    assert set(p) == {"metric", "total_points", "points", "summary"}


@pytest.mark.asyncio
async def test_timeseries_empty_data_has_no_summary(monkeypatch):
    result = await _run_series(monkeypatch, {"data": []})
    assert result.ok is True
    assert result.payload["points"] == []
    assert "summary" not in result.payload


@pytest.mark.asyncio
async def test_timeseries_malformed_body_is_unavailable(monkeypatch):
    result = await _run_series(monkeypatch, {"no": "data"})
    assert result.ok is False
    assert result.unavailable_reason == "timeseries unreachable"
