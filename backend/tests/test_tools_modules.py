"""Module-backed read tools: frozen module contracts and fail-safe paths.

Each section freezes the contract read from the real module code:

- risk            -> Orion-LD ``Alert`` entities (orion.py).
- weather-map     -> GET /api/weather-map/stats/{parcel_id} (weather-map tiles.py).
- bioorchestrator -> GET /api/graph/agriculture/recommend/evidence (recommend.py).
- cue             -> GET /api/modules/cue/productos-ropo (cue_api.py / cue_client.py).

The mutations listed in the task each have a test that goes red when the
corresponding property is broken (dropped timeout, tenant id leak, substring
authorization, missing alert attributes, 500/HTML bodies).
"""

from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.agent import registry
from app.domain.session import SessionContext
from app.tools.adapters import bioorch, cue, orion, weather
from tests.conftest import requires_db


def _ctx(tenant: str = "tenant_a") -> SessionContext:
    return SessionContext(
        tenant_id=tenant,
        user_id="user_a",
        roles=("Farmer",),
        channel="telegram",
        channel_user_id="42",
        trace_id="t-1",
    )


# --- Shared HTTP fakes --------------------------------------------------------


class _RecordingResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _RecordingClient:
    def __init__(self, body, timeout=None):
        self.captured = {"timeout": timeout}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        self.captured["url"] = url
        self.captured["params"] = params
        self.captured["headers"] = headers
        return _RecordingResponse(self._body)


class _HttpErrorResponse:
    def raise_for_status(self):
        raise httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("GET", "http://module.invalid"),
            response=httpx.Response(
                500, request=httpx.Request("GET", "http://module.invalid")
            ),
        )


class _HtmlResponse:
    def raise_for_status(self):
        pass

    def json(self):
        raise ValueError("not json")


class _FailingClient:
    def __init__(self, response, timeout=None):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        return self._response


# --- 1. risk: get_active_alerts ----------------------------------------------

# Frozen from nkz-module-risk/backend/app/alerts/contract.py build_alert_entity:
# category, alertType, severity, refEntity (Relationship), refEntityType,
# confidence, evaluationData, status and observedAt (plus optional
# probabilityScore, which the adapter intentionally does not expose).
ALERTS = [
    {
        "@context": "https://context.example.invalid/ngsi-ld-context.json",
        "id": "urn:ngsi-ld:Alert:tenant_a:frost:p1",
        "type": "Alert",
        "category": {"type": "Property", "value": "weather"},
        "alertType": {"type": "Property", "value": "frost"},
        "severity": {"type": "Property", "value": "high"},
        "refEntity": {"type": "Relationship", "object": "urn:ngsi-ld:AgriParcel:p1"},
        "refEntityType": {"type": "Property", "value": "AgriParcel"},
        "confidence": {"type": "Property", "value": 0.95},
        "evaluationData": {"type": "Property", "value": {"temp": -3.0}},
        "status": {"type": "Property", "value": "active"},
        "observedAt": "2026-01-01T00:00:00Z",
    },
    {
        "@context": "https://context.example.invalid/ngsi-ld-context.json",
        "id": "urn:ngsi-ld:Alert:tenant_a:water:p2",
        "type": "Alert",
        "category": {"type": "Property", "value": "weather"},
        "alertType": {"type": "Property", "value": "water"},
        "severity": {"type": "Property", "value": "medium"},
        "refEntity": {"type": "Relationship", "object": "urn:ngsi-ld:AgriParcel:p2"},
        "refEntityType": {"type": "Property", "value": "AgriParcel"},
        "confidence": {"type": "Property", "value": 0.8},
        "evaluationData": {"type": "Property", "value": {}},
        "status": {"type": "Property", "value": "resolved"},
        "observedAt": "2026-01-02T00:00:00Z",
        "internal_note": {"type": "Property", "value": "must not leak"},
    },
]

ALERTS_MISSING = [
    {
        "id": "urn:ngsi-ld:Alert:a3",
        "type": "Alert",
        "severity": {"type": "Property", "value": "low"},
        "status": {"type": "Property", "value": "active"},
    },
]


class _AlertOrionClient:
    def __init__(self, tenant_id, entities):
        self.tenant_id = tenant_id
        self.entities = entities
        self.calls = []

    async def query_entities(self, **kwargs):
        self.calls.append(kwargs)
        return self.entities

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_get_active_alerts_freezes_surface_scopes_and_filters(monkeypatch):
    client = _AlertOrionClient("tenant_a", ALERTS)
    monkeypatch.setattr(orion, "OrionClient", lambda tenant_id: client)

    result = await orion.get_active_alerts(_ctx("tenant_a"), orion.GetActiveAlertsArgs())

    assert result.ok is True
    assert client.tenant_id == "tenant_a"
    assert client.calls == [{"type": "Alert", "limit": 50}]

    # Mutation (a): the resolved Alert must be filtered out here. Drop the
    # status == "active" filter and payload grows to two alerts (this goes red).
    assert len(result.payload) == 1
    assert result.payload[0] == {
        "id": "urn:ngsi-ld:Alert:tenant_a:frost:p1",
        "category": "weather",
        "alertType": "frost",
        "severity": "high",
        "status": "active",
        "observedAt": "2026-01-01T00:00:00Z",
        "refEntity": "urn:ngsi-ld:AgriParcel:p1",
    }
    # Frozen surface only: a free dict would leak attributes nobody reviewed.
    assert set(result.payload[0].keys()) == {
        "id",
        "category",
        "alertType",
        "severity",
        "status",
        "observedAt",
        "refEntity",
    }
    assert "internal_note" not in result.payload[0]
    assert "refEntityType" not in result.payload[0]
    assert "confidence" not in result.payload[0]
    assert "evaluationData" not in result.payload[0]


@pytest.mark.asyncio
async def test_get_active_alerts_missing_attrs_become_none(monkeypatch):
    monkeypatch.setattr(
        orion, "OrionClient", lambda tenant_id: _AlertOrionClient(tenant_id, ALERTS_MISSING)
    )

    result = await orion.get_active_alerts(_ctx(), orion.GetActiveAlertsArgs())

    assert result.ok is True
    assert result.payload[0]["category"] is None
    assert result.payload[0]["alertType"] is None
    assert result.payload[0]["observedAt"] is None
    assert result.payload[0]["refEntity"] is None
    assert result.payload[0]["severity"] == "low"
    assert result.payload[0]["status"] == "active"


@pytest.mark.asyncio
async def test_get_active_alerts_orion_error_returns_unavailable(monkeypatch):
    class _Exploding:
        def __init__(self, tenant_id):
            pass

        async def query_entities(self, **_):
            raise RuntimeError("boom")

        async def close(self):
            pass

    monkeypatch.setattr(orion, "OrionClient", _Exploding)
    result = await orion.get_active_alerts(_ctx(), orion.GetActiveAlertsArgs())
    assert result.ok is False
    assert result.payload is None
    assert result.unavailable_reason == "alert list unavailable"


# --- 2. weather-map: get_weather_conditions -----------------------------------

# Point-in-time /stats body from nkz-module-weather-map/backend/app/stats.py:
# {parcel_geojson, date, metrics} — no daily series, no dataFidelity.
WEATHER_BODY = {
    "parcel_geojson": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
    "date": "2026-06-13",
    "metrics": {
        "temperature_avg": {"mean": 18.4, "histogram": [1, 2, 3], "pixel_count": 9},
        "eto": {"mean": 4.2},
        "not_a_metric": {"mean": 1.0},
    },
}


def _weather_settings(url: str = "http://weather.invalid"):
    return SimpleNamespace(weather_map_url=url, hmac_secret="test-secret")


@pytest.mark.asyncio
async def test_get_weather_conditions_sends_tenant_and_user_headers(monkeypatch):
    created = {}

    def factory(timeout=None):
        client = _RecordingClient(WEATHER_BODY, timeout=timeout)
        created["client"] = client
        return client

    monkeypatch.setattr(weather.httpx, "AsyncClient", factory)
    monkeypatch.setattr(weather, "get_settings", lambda: _weather_settings())

    result = await weather.get_weather_conditions(
        _ctx("tenant_x"),
        weather.GetWeatherConditionsArgs(parcel_id="urn:ngsi-ld:AgriParcel:p1"),
    )

    assert result.ok is True
    # Timeout is mandatory: dropping it here goes red.
    client = created["client"]
    assert client.captured["timeout"] == 8.0
    assert client.captured["headers"]["X-Tenant-ID"] == "tenant_x"
    # Mutation (b): weather-map require_tenant needs X-User-ID too; dropping
    # this header here makes the assertion go red.
    assert client.captured["headers"]["X-User-ID"] == "user_a"
    assert client.captured["headers"]["X-Auth-Signature"]
    assert "urn:ngsi-ld:AgriParcel:p1" in client.captured["url"]
    assert client.captured["params"]["metrics"] == ",".join(weather.FROZEN_METRICS)


@pytest.mark.asyncio
async def test_get_weather_conditions_exposes_point_in_time_date(monkeypatch):
    monkeypatch.setattr(
        weather.httpx,
        "AsyncClient",
        lambda timeout=None: _RecordingClient(WEATHER_BODY, timeout=timeout),
    )
    monkeypatch.setattr(weather, "get_settings", lambda: _weather_settings())

    result = await weather.get_weather_conditions(
        _ctx(), weather.GetWeatherConditionsArgs(parcel_id="urn:ngsi-ld:AgriParcel:p1")
    )

    assert result.ok is True
    assert result.payload["date"] == "2026-06-13"
    assert result.payload["metrics"]["eto"]["mean"] == 4.2
    # The endpoint is point-in-time: no daily series, no dataFidelity.
    assert "daily" not in result.payload
    # Frozen surface: no polygon, no histogram, no unrequested metric.
    assert set(result.payload) == {"date", "metrics"}
    assert "parcel_geojson" not in result.payload
    assert result.payload["metrics"]["temperature_avg"] == {"mean": 18.4}
    assert "not_a_metric" not in result.payload["metrics"]
    assert "dataFidelity" not in result.payload
    assert (
        result.citations[0].detail
        == "parcel=urn:ngsi-ld:AgriParcel:p1 date=2026-06-13"
    )


@pytest.mark.asyncio
async def test_get_weather_conditions_failures_are_unavailable(monkeypatch):
    monkeypatch.setattr(
        weather.httpx,
        "AsyncClient",
        lambda timeout=None: _FailingClient(response=_HttpErrorResponse()),
    )
    monkeypatch.setattr(weather, "get_settings", lambda: _weather_settings())
    result = await weather.get_weather_conditions(
        _ctx(), weather.GetWeatherConditionsArgs(parcel_id="urn:ngsi-ld:AgriParcel:p1")
    )
    assert result.ok is False
    assert result.payload is None
    assert result.unavailable_reason == "weather map unreachable"


@pytest.mark.asyncio
async def test_get_weather_conditions_html_body_returns_unavailable(monkeypatch):
    monkeypatch.setattr(
        weather.httpx,
        "AsyncClient",
        lambda timeout=None: _FailingClient(response=_HtmlResponse()),
    )
    monkeypatch.setattr(weather, "get_settings", lambda: _weather_settings())
    result = await weather.get_weather_conditions(
        _ctx(), weather.GetWeatherConditionsArgs(parcel_id="urn:ngsi-ld:AgriParcel:p1")
    )
    assert result.ok is False
    assert result.payload is None


@pytest.mark.asyncio
async def test_get_weather_conditions_without_url_returns_unavailable(monkeypatch):
    monkeypatch.setattr(weather, "get_settings", lambda: _weather_settings(url=""))
    result = await weather.get_weather_conditions(
        _ctx(), weather.GetWeatherConditionsArgs(parcel_id="urn:ngsi-ld:AgriParcel:p1")
    )
    assert result.ok is False
    assert result.unavailable_reason == "weather map unreachable"


# --- 3. bioorchestrator: get_crop_trial_evidence ------------------------------

BIOORCH_BODY = {
    "items": [
        {
            "trial_id": "t1",
            "variety": "Picual",
            "site": "site-a",
            "year": 2020,
            "yield_kg_ha": 4.2,
            "irrigation_regime": "regadío",
            "production_system": "conventional",
            "source_id": "src-1",
            "confidence": "high",
            "tier": "field",
            "basis": "grain",
            "irrigation_status": "stated",
        }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20,
    "purpose": "main",
    "tier": "field",
    "attributions": [
        {
            "source_id": "src-1",
            "text": "Credit line",
            "url": "https://example.invalid",
            "licence_id": "CC-BY",
            "licence_url": "https://example.invalid/licence",
        }
    ],
    "unexpected_key": "must not leak",
}


def _bioorch_settings(url: str = "http://bioorch.invalid"):
    return SimpleNamespace(bioorch_api_url=url)


@pytest.mark.asyncio
async def test_get_crop_trial_evidence_never_sends_tenant_params(monkeypatch):
    created = {}

    def factory(timeout=None):
        client = _RecordingClient(BIOORCH_BODY, timeout=timeout)
        created["client"] = client
        return client

    monkeypatch.setattr(bioorch.httpx, "AsyncClient", factory)
    monkeypatch.setattr(bioorch, "get_settings", lambda: _bioorch_settings())

    result = await bioorch.get_crop_trial_evidence(
        _ctx("tenant_x"),
        bioorch.GetCropTrialEvidenceArgs(
            crop="TRZAX", climate="Cfb", water_regime="secano"
        ),
    )

    assert result.ok is True
    client = created["client"]
    assert client.captured["timeout"] == 8.0
    assert client.captured["params"]["crop"] == "TRZAX"
    assert client.captured["params"]["climate_class"] == "Cfb"
    assert client.captured["params"]["irrigation_regime"] == "secano"
    assert "country" not in client.captured["params"]
    # Mutation: the endpoint rejects these with 422 — the adapter must never
    # put a tenant/parcel selector on the query.
    assert "parcel_id" not in client.captured["params"]
    assert "tenant_id" not in client.captured["params"]
    assert "X-Tenant-ID" not in (client.captured.get("headers") or {})


@pytest.mark.asyncio
async def test_get_crop_trial_evidence_freezes_response_keys(monkeypatch):
    monkeypatch.setattr(
        bioorch.httpx,
        "AsyncClient",
        lambda timeout=None: _RecordingClient(BIOORCH_BODY, timeout=timeout),
    )
    monkeypatch.setattr(bioorch, "get_settings", lambda: _bioorch_settings())

    result = await bioorch.get_crop_trial_evidence(
        _ctx(), bioorch.GetCropTrialEvidenceArgs(crop="TRZAX", climate="Cfb")
    )

    assert result.ok is True
    assert set(result.payload.keys()) == {
        "items",
        "total",
        "page",
        "page_size",
        "purpose",
        "tier",
        "zone_pool",
        "attributions",
    }
    assert "unexpected_key" not in result.payload
    assert set(result.payload["items"][0].keys()) == {
        "trial_id",
        "variety",
        "site",
        "year",
        "yield_kg_ha",
        "irrigation_regime",
        "production_system",
        "source_id",
        "confidence",
        "tier",
        "basis",
        "irrigation_status",
    }


def test_get_crop_trial_evidence_validation_matches_endpoint():
    # Real recommend.py patterns: crop ^[A-Za-z0-9]{2,6}$, climate
    # ^[A-Z][A-Za-z]{0,2}$, water_regime literal, country ^[A-Z]{2}$.
    bioorch.GetCropTrialEvidenceArgs(
        crop="TRZAX", climate="Cfb", water_regime="secano"
    )

    for bad_crop in ("c", "cebada!", "cebada temprana", "cebada1x7"):
        with pytest.raises(ValidationError):
            bioorch.GetCropTrialEvidenceArgs(crop=bad_crop, climate="Cfb")

    with pytest.raises(ValidationError):
        bioorch.GetCropTrialEvidenceArgs(crop="TRZAX", climate="cfb")
    with pytest.raises(ValidationError):
        bioorch.GetCropTrialEvidenceArgs(crop="TRZAX", climate="Cfb", water_regime="riego")
    with pytest.raises(ValidationError):
        bioorch.GetCropTrialEvidenceArgs(crop="TRZAX", climate="Cfb", water_regime="riego")


@pytest.mark.asyncio
async def test_get_crop_trial_evidence_failures_are_unavailable(monkeypatch):
    monkeypatch.setattr(
        bioorch.httpx,
        "AsyncClient",
        lambda timeout=None: _FailingClient(response=_HttpErrorResponse()),
    )
    monkeypatch.setattr(bioorch, "get_settings", lambda: _bioorch_settings())
    result = await bioorch.get_crop_trial_evidence(
        _ctx(), bioorch.GetCropTrialEvidenceArgs(crop="TRZAX", climate="Cfb")
    )
    assert result.ok is False
    assert result.payload is None
    assert result.unavailable_reason == "advisory unavailable"


@pytest.mark.asyncio
async def test_get_crop_trial_evidence_html_body_returns_unavailable(monkeypatch):
    monkeypatch.setattr(
        bioorch.httpx,
        "AsyncClient",
        lambda timeout=None: _FailingClient(response=_HtmlResponse()),
    )
    monkeypatch.setattr(bioorch, "get_settings", lambda: _bioorch_settings())
    result = await bioorch.get_crop_trial_evidence(
        _ctx(), bioorch.GetCropTrialEvidenceArgs(crop="TRZAX", climate="Cfb")
    )
    assert result.ok is False
    assert result.payload is None
    assert result.unavailable_reason == "advisory unavailable"


@pytest.mark.asyncio
async def test_get_crop_trial_evidence_without_url_returns_unavailable(monkeypatch):
    monkeypatch.setattr(bioorch, "get_settings", lambda: _bioorch_settings(url=""))
    result = await bioorch.get_crop_trial_evidence(
        _ctx(), bioorch.GetCropTrialEvidenceArgs(crop="TRZAX", climate="Cfb")
    )
    assert result.ok is False
    assert result.unavailable_reason == "advisory unavailable"


# --- 4. cue: check_substance_authorization ------------------------------------

CUE_PRODUCTS = [
    {
        "numero_registro": "1",
        "nombre_comercial": "Roundup",
        "ingrediente_activo": "Glifosato 36% SL",
    },
    {
        "numero_registro": "2",
        "nombre_comercial": "Folicur",
        "ingrediente_activo": "Tebuconazol",
    },
]


def _cue_settings(url: str = "http://cue.invalid", secret: str = "test-internal-secret"):
    return SimpleNamespace(cue_api_url=url, internal_service_secret=secret)


@pytest.mark.asyncio
async def test_check_substance_authorized_on_token_match(monkeypatch):
    created = {}

    def factory(timeout=None):
        client = _RecordingClient(CUE_PRODUCTS, timeout=timeout)
        created["client"] = client
        return client

    monkeypatch.setattr(cue.httpx, "AsyncClient", factory)
    monkeypatch.setattr(cue, "get_settings", lambda: _cue_settings())

    result = await cue.check_substance_authorization(
        _ctx("tenant_x"),
        cue.CheckSubstanceAuthorizationArgs(substance="glifosato", crop="trigo"),
    )

    assert result.ok is True
    client = created["client"]
    assert client.captured["timeout"] == 8.0
    assert client.captured["headers"]["X-Tenant-ID"] == "tenant_x"
    assert client.captured["headers"]["X-Internal-Service-Secret"] == "test-internal-secret"
    assert client.captured["params"] == {
        "cultivo": "trigo", "estado": "autorizado", "q": "glifosato",
    }

    payload = result.payload
    assert payload["status"] == "authorized"
    assert payload["source"] == "ROPO via CUE"
    assert payload["retrieved_at"]
    assert payload["disclaimer"]


class _ScriptedCueClient:
    """Answers each successive GET with the next scripted body; records params."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append(dict(params))
        return _RecordingResponse(self._bodies.pop(0))


async def _run_cue(monkeypatch, bodies, substance="glifosato"):
    client = _ScriptedCueClient(bodies)
    monkeypatch.setattr(cue.httpx, "AsyncClient", lambda timeout=None: client)
    monkeypatch.setattr(cue, "get_settings", lambda: _cue_settings())
    result = await cue.check_substance_authorization(
        _ctx(), cue.CheckSubstanceAuthorizationArgs(substance=substance, crop="trigo")
    )
    return result.payload, client


@pytest.mark.asyncio
async def test_cue_narrowed_query_with_match_is_authorized(monkeypatch):
    payload, client = await _run_cue(monkeypatch, [CUE_PRODUCTS])
    assert payload["status"] == "authorized"
    assert len(client.calls) == 1
    assert client.calls[0]["q"] == "glifosato"


@pytest.mark.asyncio
async def test_cue_full_page_without_match_is_unknown_truncated(monkeypatch):
    page = [
        {"nombre_comercial": f"Otro {i}", "ingrediente_activo": "Otro"}
        for i in range(200)
    ]
    payload, _ = await _run_cue(monkeypatch, [page])
    assert payload["status"] == "unknown"
    assert payload["detail"] == "registry result truncated"


@pytest.mark.asyncio
async def test_cue_empty_both_queries_is_unknown_crop(monkeypatch):
    payload, client = await _run_cue(monkeypatch, [[], []])
    assert payload["status"] == "unknown"
    assert payload["detail"] == "crop name not recognised by the registry"
    assert "q" not in client.calls[1]


@pytest.mark.asyncio
async def test_cue_empty_substance_but_known_crop_is_not_authorized(monkeypatch):
    payload, client = await _run_cue(monkeypatch, [[], CUE_PRODUCTS])
    assert payload["status"] == "not_authorized"
    assert "q" in client.calls[0] and "q" not in client.calls[1]


@pytest.mark.asyncio
async def test_check_substance_substring_never_authorizes(monkeypatch):
    """Mutation (c): "azol" is a substring of "tebuconazol", not the substance."""
    monkeypatch.setattr(
        cue.httpx,
        "AsyncClient",
        lambda timeout=None: _RecordingClient(CUE_PRODUCTS, timeout=timeout),
    )
    monkeypatch.setattr(cue, "get_settings", lambda: _cue_settings())

    result = await cue.check_substance_authorization(
        _ctx(), cue.CheckSubstanceAuthorizationArgs(substance="azol", crop="trigo")
    )

    assert result.ok is True
    assert result.payload["status"] == "not_authorized"


@pytest.mark.asyncio
async def test_check_substance_not_found_is_not_authorized(monkeypatch):
    monkeypatch.setattr(
        cue.httpx,
        "AsyncClient",
        lambda timeout=None: _RecordingClient(CUE_PRODUCTS, timeout=timeout),
    )
    monkeypatch.setattr(cue, "get_settings", lambda: _cue_settings())

    result = await cue.check_substance_authorization(
        _ctx(), cue.CheckSubstanceAuthorizationArgs(substance="mancozeb", crop="trigo")
    )

    assert result.payload["status"] == "not_authorized"
    assert result.payload["status"] != "authorized"


@pytest.mark.asyncio
async def test_check_substance_non_es_country_is_unknown(monkeypatch):
    monkeypatch.setattr(cue, "get_settings", lambda: _cue_settings())
    result = await cue.check_substance_authorization(
        _ctx(), cue.CheckSubstanceAuthorizationArgs(substance="glifosato", crop="trigo", country="FR")
    )
    assert result.payload["status"] == "unknown"
    assert result.payload["detail"] == "no registry for country"


@pytest.mark.asyncio
async def test_check_substance_cue_down_is_unknown(monkeypatch):
    monkeypatch.setattr(
        cue.httpx,
        "AsyncClient",
        lambda timeout=None: _FailingClient(response=_HttpErrorResponse()),
    )
    monkeypatch.setattr(cue, "get_settings", lambda: _cue_settings())
    result = await cue.check_substance_authorization(
        _ctx(), cue.CheckSubstanceAuthorizationArgs(substance="glifosato", crop="trigo")
    )
    assert result.payload["status"] == "unknown"
    assert result.payload["detail"]


@pytest.mark.asyncio
async def test_check_substance_malformed_body_is_unknown(monkeypatch):
    monkeypatch.setattr(
        cue.httpx,
        "AsyncClient",
        lambda timeout=None: _RecordingClient({"not": "a list"}, timeout=timeout),
    )
    monkeypatch.setattr(cue, "get_settings", lambda: _cue_settings())
    result = await cue.check_substance_authorization(
        _ctx(), cue.CheckSubstanceAuthorizationArgs(substance="glifosato", crop="trigo")
    )
    assert result.payload["status"] == "unknown"
    assert result.payload["detail"] == "registry response malformed"


@pytest.mark.asyncio
async def test_check_substance_html_body_is_unknown(monkeypatch):
    monkeypatch.setattr(
        cue.httpx,
        "AsyncClient",
        lambda timeout=None: _FailingClient(response=_HtmlResponse()),
    )
    monkeypatch.setattr(cue, "get_settings", lambda: _cue_settings())
    result = await cue.check_substance_authorization(
        _ctx(), cue.CheckSubstanceAuthorizationArgs(substance="glifosato", crop="trigo")
    )
    assert result.payload["status"] == "unknown"
    assert result.payload["detail"] == "registry unreachable"


@pytest.mark.asyncio
async def test_check_substance_without_url_is_unknown(monkeypatch):
    monkeypatch.setattr(cue, "get_settings", lambda: _cue_settings(url=""))
    result = await cue.check_substance_authorization(
        _ctx(), cue.CheckSubstanceAuthorizationArgs(substance="glifosato", crop="trigo")
    )
    assert result.payload["status"] == "unknown"


def test_check_substance_bounds_rejected():
    with pytest.raises(ValidationError):
        cue.CheckSubstanceAuthorizationArgs(substance="", crop="trigo")
    with pytest.raises(ValidationError):
        cue.CheckSubstanceAuthorizationArgs(substance="x" * 129, crop="trigo")
    with pytest.raises(ValidationError):
        cue.CheckSubstanceAuthorizationArgs(substance="glifosato", crop="x" * 65)


# --- Registry: module tools are filtered by tenant installs --------------------


@requires_db
@pytest.mark.usefixtures("configured_service_urls")
@pytest.mark.asyncio
async def test_module_tools_filtered_out_when_tenant_has_no_installs(db_pool):
    registry.clear_cache()
    tools = await registry.tools_for("tenant_no_module_rows")

    names = {t.name for t in tools}
    assert names == {"get_parcels", "get_parcel_snapshot", "get_timeseries"}
    assert "get_active_alerts" not in names
    assert "get_weather_conditions" not in names
    assert "get_crop_trial_evidence" not in names
    assert "check_substance_authorization" not in names


def test_get_crop_trial_evidence_requires_climate():
    """The real endpoint rejects the query without climate_class; our schema
    mirrors that requirement so no guaranteed-422 request can be built."""
    with pytest.raises(ValidationError):
        bioorch.GetCropTrialEvidenceArgs(crop="TRZAX")


# --- Rare-but-valid response shapes: degrade, never crash. ---




# --- Rare-but-valid response shapes: degrade, never crash. -------------------


class _JsonResponse:
    """Valid HTTP 200 whose JSON body is a strange-but-parseable shape."""

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_bioorch_dict_without_items_degrades(monkeypatch):
    monkeypatch.setattr(
        bioorch.httpx, "AsyncClient", lambda **kw: _FailingClient(_JsonResponse({"total": 0}))
    )
    monkeypatch.setattr(bioorch, "get_settings", lambda: _bioorch_settings())
    result = await bioorch.get_crop_trial_evidence(
        _ctx(), bioorch.GetCropTrialEvidenceArgs(crop="TRZAX", climate="Cfb")
    )
    assert result.ok is False
    assert result.unavailable_reason == "advisory unavailable"


@pytest.mark.asyncio
async def test_weather_non_dict_body_degrades(monkeypatch):
    monkeypatch.setattr(
        weather.httpx,
        "AsyncClient",
        lambda **kw: _FailingClient(_JsonResponse(["not", "a", "dict"])),
    )
    monkeypatch.setattr(weather, "get_settings", lambda: _weather_settings())
    result = await weather.get_weather_conditions(
        _ctx(), weather.GetWeatherConditionsArgs(parcel_id="urn:ngsi-ld:AgriParcel:p1")
    )
    assert result.ok is False
    assert result.payload is None


@pytest.mark.asyncio
async def test_empty_alert_list_is_ok_empty(monkeypatch):
    class _EmptyOrion:
        def __init__(self, tenant_id):
            pass

        async def query_entities(self, **_):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(orion, "OrionClient", _EmptyOrion)
    result = await orion.get_active_alerts(_ctx(), orion.GetActiveAlertsArgs())
    assert result.ok is True
    assert result.payload == []
