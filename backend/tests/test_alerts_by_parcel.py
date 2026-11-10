"""get_active_alerts parcel filter and plain/confidence reading."""

from __future__ import annotations

import pytest

from app.domain.session import SessionContext
from app.tools.adapters import orion

P1 = "urn:ngsi-ld:AgriParcel:p1"
P2 = "urn:ngsi-ld:AgriParcel:p2"


def _ctx() -> SessionContext:
    return SessionContext("acme", "u", ("Farmer",), "mobile", "1", "t")


def _alert(i, parcel, **extra):
    return {
        "id": f"urn:ngsi-ld:Alert:acme:{i}",
        "type": "Alert",
        "status": {"type": "Property", "value": "active"},
        "refEntity": {"type": "Relationship", "object": parcel},
        **extra,
    }


class _Client:
    def __init__(self, entities):
        self.entities = entities

    async def query_entities(self, **kw):
        return self.entities if kw.get("offset", 0) == 0 else []

    async def close(self):
        pass


async def _run(monkeypatch, entities, **args):
    monkeypatch.setattr(orion, "OrionClient", lambda tenant_id: _Client(entities))
    return await orion.get_active_alerts(_ctx(), orion.GetActiveAlertsArgs(**args))


@pytest.mark.asyncio
async def test_parcel_filter_keeps_matching_ref_entity(monkeypatch):
    ents = [_alert("a", P1), _alert("b", P2), _alert("c", P1)]
    res = await _run(monkeypatch, ents, parcel_id=P1)
    assert [a["id"][-1] for a in res.payload] == ["a", "c"]
    res = await _run(monkeypatch, ents)
    assert len(res.payload) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["plain", "nkz:plain", "https://nkz-os.org/ns/plain"]
)
async def test_plain_read_under_all_three_forms(monkeypatch, key):
    ent = _alert("a", P1, **{key: {"type": "Property", "value": {"en": "Frost", "es": "Helada"}}})
    res = await _run(monkeypatch, [ent])
    assert res.payload[0]["plain"] == "Helada"


@pytest.mark.asyncio
async def test_plain_falls_back_to_first_text_and_missing_is_none(monkeypatch):
    ent = _alert("a", P1, plain={"type": "Property", "value": {"en": "Frost"}})
    res = await _run(monkeypatch, [ent, _alert("b", P1)])
    assert res.payload[0]["plain"] == "Frost"
    assert res.payload[1]["plain"] is None and res.payload[1]["confidence"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["confidence", "nkz:confidence", "https://nkz-os.org/ns/confidence"])
async def test_confidence_under_all_forms(monkeypatch, key):
    ent = _alert("a", P1, **{key: {"type": "Property", "value": 0.8}})
    res = await _run(monkeypatch, [ent])
    assert res.payload[0]["confidence"] == 0.8
