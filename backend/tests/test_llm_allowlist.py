import logging

import pytest

from app.llm.allowlist import normalize_origin, parse


def test_empty_allows_nothing():
    for raw in ("", "   ", ",,"):
        al = parse(raw)
        assert al.entries() == []
        assert not al.allows("provider-a", None)
        assert not al.allows("openai", "https://llm.example.test")


def test_provider_only_allows_default_endpoint_only():
    al = parse("provider-a")
    assert al.allows("provider-a", None)
    assert al.allows("provider-a", "")
    assert not al.allows("provider-a", "https://x.example.test")
    assert not al.allows("openai", None)


def test_provider_with_origin_does_not_allow_default_endpoint():
    al = parse("openai@https://llm.example.test")
    assert not al.allows("openai", None)


def test_https_origin_matching():
    al = parse("openai@https://llm.example.test")
    assert al.allows("openai", "https://llm.example.test/v1")
    assert al.allows("openai", "https://llm.example.test:443/v1")
    assert al.allows("openai", "https://LLM.Example.Test/v1")
    assert al.allows("openai", "https://llm.example.test")
    assert not al.allows("openai", "http://llm.example.test/v1")
    assert not al.allows("openai", "https://llm.example.test:8443/v1")
    assert not al.allows("openai", "https://evil.llm.example.test/v1")
    assert not al.allows("openai", "https://llm.example.test.evil.test/v1")
    assert not al.allows("provider-c", "https://llm.example.test/v1")


def test_explicit_default_port_in_entry():
    al = parse("openai@https://llm.example.test:443")
    assert al.allows("openai", "https://llm.example.test/v1")


def test_http_with_port():
    al = parse("openai@http://10.0.0.5:8000")
    assert al.allows("openai", "http://10.0.0.5:8000/v1")
    assert not al.allows("openai", "http://10.0.0.5:8001/v1")
    assert not al.allows("openai", "https://10.0.0.5:8000/v1")
    assert not al.allows("openai", "http://10.0.0.5/v1")


def test_http_default_port():
    al = parse("openai@http://llm.example.test")
    assert al.allows("openai", "http://llm.example.test:80/x")
    assert not al.allows("openai", "https://llm.example.test/x")


@pytest.mark.parametrize(
    "base",
    [
        "http://orion-ld-service:1026",
        "http://169.254.169.254/latest",
        "http://localhost:8000",
        "http://127.0.0.1",
    ],
)
def test_internal_targets_refused(base):
    al = parse("provider-a,openai@https://llm.example.test")
    assert not al.allows("openai", base)
    assert not al.allows("provider-a", base)


@pytest.mark.parametrize(
    "base",
    [
        "llm.example.test/v1",
        "//llm.example.test/v1",
        "file:///etc/passwd",
        "ftp://llm.example.test",
        "javascript:alert(1)",
        "https://",
        "https:///v1",
        "",
        "   ",
    ],
)
def test_bad_scheme_or_shape_refused(base):
    al = parse("openai@https://llm.example.test")
    assert not al.allows("openai", base)


@pytest.mark.parametrize(
    "base",
    [
        "https://user:pw@llm.example.test/v1",
        "https://user@llm.example.test/v1",
        "https://llm.example.test@evil.test/v1",
        "https://evil.test\\@llm.example.test/v1",
        "https://llm.example.test:abc/v1",
        "https://llm.example.test:99999/v1",
        "https://llm.example.test /v1",
        "https://llm.example.test\n.evil.test/",
    ],
)
def test_userinfo_and_tricks_refused(base):
    al = parse("openai@https://llm.example.test")
    assert not al.allows("openai", base)


def test_non_string_base_refused():
    al = parse("provider-a")
    assert not al.allows("provider-a", 5)  # type: ignore[arg-type]


def test_provider_case_and_whitespace():
    al = parse("  OpenAI @ https://llm.example.test , Provider-A ")
    assert al.allows(" openai ", "https://llm.example.test/v1")
    assert al.allows("PROVIDER-A", None)


def test_multiple_origins_same_provider_and_order():
    al = parse("openai@https://a.example.test,openai@http://b.example.test:81,provider-a")
    assert al.allows("openai", "https://a.example.test/v1")
    assert al.allows("openai", "http://b.example.test:81/v1")
    assert al.entries() == [
        {"provider": "openai", "origin": "https://a.example.test:443"},
        {"provider": "openai", "origin": "http://b.example.test:81"},
        {"provider": "provider-a", "origin": None},
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "@https://llm.example.test",
        "openai@",
        "openai@llm.example.test",
        "openai@file:///etc",
        "openai@https://u:p@llm.example.test",
        "openai@https://llm.example.test/v1",
        "openai@https://*.example.test",
        "open ai@https://llm.example.test",
        "a@b@https://llm.example.test",
    ],
)
def test_malformed_entries_skipped_with_warning_never_widen(raw, caplog):
    with caplog.at_level(logging.WARNING, logger="app.llm.allowlist"):
        al = parse(raw)
    assert al.entries() == []
    assert not al.allows("openai", None)
    assert not al.allows("openai", "https://llm.example.test")
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_malformed_does_not_break_valid_neighbours():
    al = parse("bad@@x,provider-a")
    assert al.allows("provider-a", None)
    assert len(al.entries()) == 1


def test_normalize_origin():
    assert normalize_origin("https://Llm.Example.Test/v1?q=1#f") == "https://llm.example.test:443"
    assert normalize_origin("http://llm.example.test:8000") == "http://llm.example.test:8000"
    assert normalize_origin("http://[::1]:8000/x") == "http://[::1]:8000"
    assert normalize_origin("llm.example.test") is None
    assert normalize_origin("file:///x") is None
    assert normalize_origin("https://u@h.example.test") is None
    assert normalize_origin("") is None
