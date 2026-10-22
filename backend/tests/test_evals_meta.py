"""Meta-tests for the synthetic eval battery (evals/).

These run the whole battery against stub providers: no database, no network,
no LLM tokens. They prove the CASES are well-formed and that the HARNESS
detects failures -- a green real-provider run is only meaningful if a
deliberately bad provider turns it red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.handlers import EMPTY_MESSAGE_TEXT, VOICE_NOT_READY_TEXT
from app.tools.catalog import available_tools
from evals import harness
from evals.fixtures import build_tools

CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "cases"
EXPECTED_FILES = {
    "core.yaml", "weather.yaml", "advisory.yaml",
    "substance.yaml", "safety.yaml", "memory.yaml",
}


@pytest.fixture(scope="module")
def cases():
    return harness.load_cases(CASES_DIR)


def _by_id(cases, case_id):
    return next(c for c in cases if c.id == case_id)


def test_case_files_present():
    assert {p.name for p in CASES_DIR.glob("*.yaml")} == EXPECTED_FILES


def test_ids_unique_and_every_case_asserts(cases):
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))
    assert all(c.has_assertion() for c in cases)


def test_tool_names_exist_in_catalogue(cases):
    real = {t.name for t in available_tools()}
    for c in cases:
        names = set(c.tools_available) | {t.name for t in c.expect.tool_calls}
        assert names <= real, (c.id, names - real)
        for forbidden in c.expect.forbidden_tools:
            assert forbidden in real or forbidden.startswith("nonexistent_"), (
                c.id, forbidden,
            )


def test_required_counts(cases):
    ids = [c.id for c in cases]
    assert sum(c.fixture_variant == "injected" for c in cases) >= 3
    assert sum("-not-installed-" in i and i.startswith("safety-") for i in ids) >= 2
    assert sum(c.kind in ("voice", "empty") for c in cases) >= 2
    assert sum(i.startswith("safety-budget-") for i in ids) >= 2
    assert sum(i.startswith("memory-") for i in ids) >= 3
    assert sum(c.lang == "eu" for c in cases) >= 3
    assert len(cases) >= 20


def test_build_tools_rejects_unknown_name():
    with pytest.raises(ValueError):
        build_tools(["no_such_tool"])


def test_build_tools_uses_real_args_models():
    real = {t.name: t for t in available_tools()}
    for tool in build_tools(list(real)):
        assert tool.args_model is real[tool.name].args_model


async def test_stub_provider_passes_every_case(cases):
    summary = await harness.run_all(cases, harness.StubProvider(), "stub")
    failed = {r.id: r.failures for r in summary.per_case if not r.passed}
    assert failed == {}
    assert summary.pass_rate == 1.0
    assert summary.tool_call_accuracy == 1.0
    assert summary.citation_rate == 1.0


async def test_hacked_provider_fails_every_injection_case(cases):
    injected = [c for c in cases if c.fixture_variant == "injected"]
    summary = await harness.run_all(injected, harness.HackedStubProvider(), "hacked")
    assert len(summary.per_case) == len(injected) >= 3
    assert all(not r.passed for r in summary.per_case)
    for r in summary.per_case:
        assert any(f.startswith("must_not_contain:") for f in r.failures), (
            r.id, r.failures,
        )


async def test_hacked_provider_is_caught_for_missing_citation(cases):
    """The reply 'HACKED' cites nothing: the citation assertion itself must fire."""
    cited = [
        c for c in cases
        if c.fixture_variant == "injected" and c.expect.reply.contains_citation
    ]
    assert cited
    for case in cited:
        result = await harness.run_case(case, harness.HackedStubProvider(), label="h")
        assert any(f.startswith("citation:") for f in result.failures), result.failures


async def test_hacked_provider_does_not_break_gate_cases(cases):
    """Gate cases never reach the model, so a bad provider cannot affect them."""
    gate = [c for c in cases if c.kind in ("voice", "empty")]
    summary = await harness.run_all(gate, harness.HackedStubProvider(), "hacked")
    assert all(r.passed for r in summary.per_case)


async def test_other_tenant_thread_is_never_delivered(cases):
    case = _by_id(cases, "memory-other-tenant-no-leak-es")
    assert case.memory_owner == "other_tenant" and case.memory
    result = await harness.run_case(case, harness.StubProvider(), label="stub")
    assert result.passed, result.failures
    assert result.extra["memory_load_keys"] == [("synthetic", "eval", "u-1")]
    assert result.extra["memory_loaded_counts"] == [0]


async def test_own_thread_is_delivered(cases):
    case = _by_id(cases, "memory-followup-with-thread-eu")
    result = await harness.run_case(case, harness.StubProvider(), label="stub")
    assert result.extra["memory_loaded_counts"] == [len(case.memory)]


async def test_voice_case_never_reaches_model_or_quota(cases):
    case = next(c for c in cases if c.kind == "voice")
    result = await harness.run_case(case, harness.StubProvider(), label="stub")
    assert result.passed, result.failures
    assert result.extra["run_turn_calls"] == 0
    assert result.extra["check_and_count_calls"] == 0
    assert result.extra["audit_outcomes"] == ["refused"]
    assert result.extra["reply"] == VOICE_NOT_READY_TEXT


async def test_empty_case_reply_constant(cases):
    case = next(c for c in cases if c.kind == "empty")
    result = await harness.run_case(case, harness.StubProvider(), label="stub")
    assert result.passed, result.failures
    assert result.extra["reply"] == EMPTY_MESSAGE_TEXT


def test_language_heuristic():
    assert harness.detect_language("el riego de la parcela para una semana") == "es"
    assert harness.detect_language("the soil is dry and your field is fine") == "en"
    assert harness.detect_language("zure lursail da eta eguraldi ona dago") == "eu"
    assert harness.detect_language("HACKED") is None
