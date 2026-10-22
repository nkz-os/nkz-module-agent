"""Completeness guard: every Settings field is declared everywhere an operator
configures the module (env.example, the k8s template, docker-compose).

No exception list on purpose: a setting that exists but is undeclared in a
deployment surface is a knob nobody can find, and one declared but not in
Settings is a knob that does nothing.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _expected() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def _assert_same(found: set[str], surface: str) -> None:
    expected = _expected()
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    assert not missing and not extra, (
        f"{surface} out of sync with Settings: missing={missing} extra={extra}"
    )


# Budget and quota keys whose VALUES must match the Settings defaults on every
# surface: a template that says 8000 while the code says 4000 is a silent
# difference between what an operator reads and what runs.
_VALUE_KEYS = (
    "MAX_ITERATIONS",
    "MAX_TOOL_CALLS",
    "MAX_TOKENS_PER_TURN",
    "TURN_TIMEOUT_SECONDS",
    "MAX_TURNS_PER_TENANT_DAY",
    "MAX_TURNS_PER_ACCOUNT_HOUR",
)


def _assert_values(found: dict[str, str], surface: str) -> None:
    defaults = Settings()
    wrong = {
        key: (found.get(key), getattr(defaults, key.lower()))
        for key in _VALUE_KEYS
        if str(found.get(key)) != str(getattr(defaults, key.lower()))
    }
    assert not wrong, f"{surface} budget/quota values differ from Settings: {wrong}"


def _env_example_pairs() -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in (REPO_ROOT / "backend" / "env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def test_env_example_declares_every_setting():
    _assert_same(set(_env_example_pairs()), "backend/env.example")


def test_env_example_budget_values_match_settings():
    _assert_values(_env_example_pairs(), "backend/env.example")


def test_k8s_template_declares_every_setting():
    docs = yaml.safe_load_all((REPO_ROOT / "k8s" / "backend-deployment.yaml").read_text())
    deployment = next(
        d for d in docs
        if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "agent-backend"
    )
    containers = deployment["spec"]["template"]["spec"]["containers"]
    container = next(c for c in containers if c["name"] == "backend")
    _assert_same({e["name"] for e in container["env"]}, "k8s/backend-deployment.yaml")
    _assert_values(
        {e["name"]: e["value"] for e in container["env"] if "value" in e},
        "k8s/backend-deployment.yaml",
    )


def test_docker_compose_declares_every_setting():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    entries = compose["services"]["backend"]["environment"]
    _assert_same({e.split("=", 1)[0] for e in entries}, "docker-compose.yml")
    _assert_values(dict(e.split("=", 1) for e in entries), "docker-compose.yml")
