"""The schema is authored once and copied into the platform's numbered
migrations. This guards the copy, and skips where the sibling repo is absent
(module CI checks out only this repo)."""

import pathlib

import pytest

SCHEMA = pathlib.Path(__file__).resolve().parent / "fixtures" / "schema.sql"
CANONICAL = (
    pathlib.Path(__file__).resolve().parents[3]
    / "nkz" / "config" / "timescaledb" / "migrations"
    / "101_agent_channel_links.sql"
)

SCHEMA_102 = pathlib.Path(__file__).resolve().parent / "fixtures" / "schema_102.sql"
CANONICAL_102 = (
    pathlib.Path(__file__).resolve().parents[3]
    / "nkz" / "config" / "timescaledb" / "migrations"
    / "102_agent_turn_audit.sql"
)


@pytest.mark.skipif(not CANONICAL.exists(), reason="platform repo not checked out")
def test_schema_matches_platform_migration():
    assert SCHEMA.read_text() == CANONICAL.read_text(), (
        "schema.sql and the numbered migration have diverged; copy one over the other"
    )


@pytest.mark.skipif(not CANONICAL_102.exists(), reason="platform repo not checked out")
def test_schema_102_matches_platform_migration():
    assert SCHEMA_102.read_text() == CANONICAL_102.read_text(), (
        "schema_102.sql and the numbered migration have diverged; copy one over the other"
    )
