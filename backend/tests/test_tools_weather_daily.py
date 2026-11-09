"""Parcel daily weather tool (weather-api /daily) and weather-map snapshot hints."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.domain.session import SessionContext
from app.tools.adapters import weather
from app.tools.catalog import available_tools

PARCEL = "urn:ngsi-ld:AgriParcel:p1"


def _ctx(tenant: str = "tenant_a") -> SessionContext:
    return SessionContext(
        tenant_id=tenant,
        user_id="user_a",
        roles=("Farmer",),
        channel="telegram",
        channel_user_id="42",
        trace_id="t-1",
    )


def _settings(api_url: str = "http://weather-api.invalid"):
    return SimpleNamespace(
        weather_api_url=api_url,
        weather_map_url="http://weather.invalid",
        hmac_secret="test-secret",
    )


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Client:
    def __init__(self, body=None, timeout=None, exc=None):
        self.captured = {"timeout": timeout}
        self._body = body
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        self.captured.update(url=url, params=params, headers=headers)
        if self._exc:
            raise self._exc
        return _Response(self._body)


def _install(monkeypatch, body=None, exc=None):
    box = {}

    def factory(timeout=None):
        box["client"] = _Client(body, timeout=timeout, exc=exc)
        return box["client"]

    monkeypatch.setattr(weather.httpx, "AsyncClient", factory)
    monkeypatch.setattr(weather, "get_settings", lambda: _settings())
    return box


def _day(date, kind, tmin, **extra):
    d = {
        "date": date,
        "kind": kind,
        "tmin_c": tmin,
        "tmax_c": 15.0,
        "precip_mm": 0.0,
        "et0_mm": 1.5,
        "hours_below_zero": 0,
        "tmin_coldest_c": tmin,
        "source": "open_meteo",
        "sources": {"tmin_c": "open_meteo"},
    }
    d.update(extra)
    return d


DAILY_BODY = {
    "local_today": "2026-11-09",
    "local_now": "2026-11-09T11:30:00+01:00",
    "forecast_issued_at": "2026-11-09T04:00:00Z",
    "missing_days": [],
    "missing_reasons": {},
    "totals": {"precip_mm": 3.0},
    "days": [
        _day("2026-11-07", "closed", 4.0),
        _day("2026-11-08", "closed", 2.5, sources={"tmin_c": "iot_sensor"}, source="iot_sensor"),
        _day("2026-11-09", "running", -0.5),
        _day("2026-11-10", "forecast", 1.0, tmin_coldest_c=-1.5),
    ],
}


async def _call(**kw):
    args = weather.GetParcelDailyWeatherArgs(parcel_id=PARCEL, **kw)
    return await weather.get_parcel_daily_weather(_ctx("tenant_x"), args)


@pytest.mark.asyncio
async def test_daily_tool_signs_and_returns_kind_source_and_date(monkeypatch):
    box = _install(monkeypatch, DAILY_BODY)
    result = await _call(past_days=3, forecast_days=2)

    assert result.ok is True
    c = box["client"].captured
    assert c["timeout"] == 8.0
    assert c["url"] == f"http://weather-api.invalid/api/weather/parcel/{PARCEL}/daily"
    assert c["headers"]["X-Tenant-ID"] == "tenant_x"
    assert c["headers"]["X-User-ID"] == "user_a"
    from nkz_platform_sdk.crypto import generate_hmac_signature
    assert c["headers"]["X-Auth-Signature"] == generate_hmac_signature(
        "test-secret", "", "tenant_x"
    )
    assert c["params"]["fill"] == "open_meteo"
    assert c["params"]["forecast_days"] == 2
    assert set(c["params"]) == {"start", "end", "fill", "forecast_days"}

    days = {d["date"]: d for d in result.payload["days"]}
    assert days["2026-11-10"]["kind"] == "forecast"
    assert days["2026-11-10"]["source"] == "open_meteo"
    assert days["2026-11-10"]["frost_warning_min_c"] == -1.5
    assert days["2026-11-09"]["frost_warning_min_c"] == -0.5
    assert "frost_warning_min_c" not in days["2026-11-07"]
    assert all("tmin_coldest_c" not in d for d in result.payload["days"])
    assert "not the expected minimum" in result.payload["field_notes"]
    assert days["2026-11-08"]["sources"]["tmin_c"] == "iot_sensor"
    assert result.payload["forecast_issued_at"] == "2026-11-09T04:00:00Z"
    assert result.payload["totals"] == {"precip_mm": 3.0}
    assert "tenant" not in str(result.citations).lower()
    assert result.citations[0].source == "weather-api:daily"


@pytest.mark.asyncio
async def test_daily_last_night_is_running_tmin_after_ten_local(monkeypatch):
    _install(monkeypatch, DAILY_BODY)
    result = await _call()
    ln = result.payload["last_night"]
    assert ln["date"] == "2026-11-09"
    assert ln["kind"] == "running"
    assert ln["tmin_c"] == -0.5


@pytest.mark.asyncio
async def test_daily_last_night_before_ten_is_latest_closed_day(monkeypatch):
    body = dict(DAILY_BODY, local_now="2026-11-09T08:15:00+01:00")
    _install(monkeypatch, body)
    result = await _call()
    ln = result.payload["last_night"]
    assert ln["date"] == "2026-11-08"
    assert ln["kind"] == "closed"
    assert ln["tmin_c"] == 2.5


@pytest.mark.asyncio
async def test_daily_without_local_now_uses_latest_closed_day(monkeypatch):
    body = {k: v for k, v in DAILY_BODY.items() if k != "local_now"}
    _install(monkeypatch, body)
    result = await _call()
    assert result.ok is True
    ln = result.payload["last_night"]
    assert (ln["date"], ln["kind"], ln["tmin_c"]) == ("2026-11-08", "closed", 2.5)
    note = result.payload["last_night_note"]
    assert "closed day 2026-11-08" in note
    assert "running night is not available" in note


@pytest.mark.asyncio
async def test_daily_without_local_now_or_closed_days_has_no_last_night(monkeypatch):
    body = {k: v for k, v in DAILY_BODY.items() if k != "local_now"}
    body["days"] = [d for d in DAILY_BODY["days"] if d["kind"] != "closed"]
    _install(monkeypatch, body)
    result = await _call()
    assert "last_night" not in result.payload
    assert result.payload["last_night_note"]


@pytest.mark.asyncio
async def test_daily_zero_forecast_days_still_requests_one_and_drops_forecast(monkeypatch):
    box = _install(monkeypatch, DAILY_BODY)
    result = await _call(forecast_days=0)
    assert box["client"].captured["params"]["forecast_days"] == 1
    assert all(d["kind"] != "forecast" for d in result.payload["days"])
    assert "forecast_unavailable" not in result.payload
    assert result.payload["last_night"]["kind"] == "running"


@pytest.mark.asyncio
async def test_daily_zero_forecast_days_without_local_today_is_not_flagged(monkeypatch):
    body = {k: v for k, v in DAILY_BODY.items() if k not in ("local_today", "local_now")}
    _install(monkeypatch, body)
    result = await _call(forecast_days=0)
    assert "forecast_unavailable" not in result.payload


@pytest.mark.asyncio
async def test_daily_without_local_today_forecast_is_unavailable(monkeypatch):
    body = {k: v for k, v in DAILY_BODY.items() if k not in ("local_today", "local_now")}
    _install(monkeypatch, body)
    result = await _call()
    assert result.ok is True
    assert all(d["kind"] != "forecast" for d in result.payload["days"])
    assert result.payload["forecast_unavailable"]


@pytest.mark.asyncio
async def test_daily_tool_timeout_returns_unavailable_not_stale(monkeypatch):
    _install(monkeypatch, exc=httpx.ReadTimeout("slow"))
    result = await _call()
    assert result.ok is False
    assert result.payload is None
    assert result.unavailable_reason == "daily weather unreachable"


@pytest.mark.asyncio
async def test_daily_tool_malformed_body_is_unavailable(monkeypatch):
    _install(monkeypatch, {"days": "nope"})
    result = await _call()
    assert result.ok is False
    assert result.payload is None


@pytest.mark.parametrize(
    "kw", [{"past_days": 31}, {"forecast_days": 16}, {"past_days": -1}, {"forecast_days": -1}]
)
def test_daily_tool_rejects_ranges_over_limit(kw):
    with pytest.raises(ValidationError):
        weather.GetParcelDailyWeatherArgs(parcel_id=PARCEL, **kw)
    weather.GetParcelDailyWeatherArgs(parcel_id=PARCEL, past_days=30, forecast_days=15)


def test_daily_tool_is_hidden_without_setting():
    tool = next(t for t in available_tools() if t.name == "get_parcel_daily_weather")
    assert tool.required_setting == "weather_api_url"
    assert tool.required_module is None


@pytest.mark.asyncio
async def test_daily_tool_unconfigured_is_unavailable(monkeypatch):
    monkeypatch.setattr(weather, "get_settings", lambda: _settings(""))
    result = await _call()
    assert result.ok is False


def test_daily_tool_description_states_model_vs_measured_and_source():
    d = weather.GET_PARCEL_DAILY_WEATHER.description.lower()
    assert "iot_sensor" in d
    assert "model" in d
    assert "source" in d and "date" in d
    assert "10:00" in d
    assert "frost_warning_min_c" in d


def test_conditions_tool_description_says_snapshot_not_measured():
    d = weather.GET_WEATHER_CONDITIONS.description.lower()
    assert "snapshot" in d
    assert "not measured" in d
    assert "spatial" in d
    assert "get_parcel_daily_weather" in d


@pytest.mark.asyncio
async def test_conditions_payload_keeps_kind_and_valid_date_when_present(monkeypatch):
    body = {
        "date": "2026-11-09",
        "kind": "forecast_snapshot",
        "valid_date": "2026-11-10",
        "metrics": {"temperature_min": {"mean": 1.0}},
    }
    monkeypatch.setattr(weather.httpx, "AsyncClient", lambda timeout=None: _Client(body, timeout))
    monkeypatch.setattr(weather, "get_settings", lambda: _settings())
    result = await weather.get_weather_conditions(
        _ctx(), weather.GetWeatherConditionsArgs(parcel_id=PARCEL)
    )
    assert result.ok is True
    assert result.payload["kind"] == "forecast_snapshot"
    assert result.payload["valid_date"] == "2026-11-10"


@pytest.mark.asyncio
async def test_conditions_payload_without_kind_has_neither_key(monkeypatch):
    body = {"date": "2026-11-09", "metrics": {}}
    monkeypatch.setattr(weather.httpx, "AsyncClient", lambda timeout=None: _Client(body, timeout))
    monkeypatch.setattr(weather, "get_settings", lambda: _settings())
    result = await weather.get_weather_conditions(
        _ctx(), weather.GetWeatherConditionsArgs(parcel_id=PARCEL)
    )
    assert "kind" not in result.payload and "valid_date" not in result.payload


@pytest.mark.asyncio
async def test_daily_http_error_is_unavailable_without_detail(monkeypatch):
    err = httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("GET", "http://weather-api.invalid/x"),
        response=httpx.Response(500, request=httpx.Request("GET", "http://x.invalid")),
    )
    _install(monkeypatch, exc=err)
    result = await _call()
    assert result.ok is False
    assert result.unavailable_reason == "daily weather unreachable"
