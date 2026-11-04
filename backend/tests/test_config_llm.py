"""The defaults here are a security and portability property, not style.

This module ships as a public repo installed on third-party servers. A default
that names our deployment — a model, an endpoint, a key — breaks their install
silently and leaks ours. Empty is the only safe default for anything that
identifies a deployment.
"""

from app.config import Settings

# Every env var these tests care about. pydantic-settings reads real OS
# environment variables and any `.env` file on disk, so a test asserting a
# default would otherwise pass or fail depending on the machine (or the
# developer's shell) it runs on, not on the code under test.
_LLM_ENV_VARS = [
    "LLM_MODEL",
    "LLM_API_BASE",
    "LLM_API_KEY",
    "LLM_TEMPERATURE",
    "MAX_ITERATIONS",
    "MAX_TOOL_CALLS",
    "MAX_TOKENS_PER_TURN",
    "TURN_TIMEOUT_SECONDS",
    "MAX_TURNS_PER_TENANT_DAY",
    "MAX_TURNS_PER_ACCOUNT_HOUR",
]


def _isolated_settings(monkeypatch) -> Settings:
    """Build a Settings instance insulated from the ambient environment.

    `_env_file=None` disables the dotenv read; clearing each var disables
    the OS-environment read. Without both, these tests would assert the
    machine's configuration instead of the code's default.
    """
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return Settings(postgres_url="postgresql://x/y", _env_file=None)


def test_llm_settings_default_to_empty(monkeypatch):
    s = _isolated_settings(monkeypatch)
    assert s.llm_model == ""
    assert s.llm_api_base == ""
    assert s.llm_api_key == ""


def test_budget_defaults_are_bounded_and_sane(monkeypatch):
    s = _isolated_settings(monkeypatch)
    assert 0 < s.max_iterations <= 10
    assert 0 < s.max_tool_calls <= 20
    assert 0 < s.max_tokens_per_turn <= 100_000
    assert 0 < s.turn_timeout_seconds <= 120


def test_quota_defaults_exist_and_are_finite(monkeypatch):
    """Fail-open is the platform rule for feature quotas; this is spend.

    A public webhook plus a paid model is a money pump, so the hard cap has a
    finite default rather than 'unlimited'. Documented deviation, spec §5.5.
    """
    s = _isolated_settings(monkeypatch)
    assert 0 < s.max_turns_per_tenant_day < 1_000_000
    assert 0 < s.max_turns_per_account_hour < 10_000


def test_temperature_defaults_low(monkeypatch):
    """An agronomic assistant should not be creative."""
    s = _isolated_settings(monkeypatch)
    assert 0.0 <= s.llm_temperature <= 0.3


def test_byok_settings_defaults(monkeypatch):
    for name in ("LLM_KEY_ENCRYPTION_KEYS", "LLM_ENDPOINT_ALLOWLIST",
                 "LLM_PLATFORM_FALLBACK", "MAX_TOKENS_PER_TURN_CEILING",
                 "MAX_TOKENS_PER_TURN"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    assert s.llm_key_encryption_keys == ""
    assert s.llm_endpoint_allowlist == ""
    assert s.llm_platform_fallback is False
    assert s.max_tokens_per_turn_ceiling == 4000


def test_ceiling_falls_back_to_legacy_env(monkeypatch):
    monkeypatch.delenv("MAX_TOKENS_PER_TURN_CEILING", raising=False)
    monkeypatch.setenv("MAX_TOKENS_PER_TURN", "2500")
    s = Settings(_env_file=None)
    assert s.max_tokens_per_turn_ceiling == 2500
    assert s.max_tokens_per_turn == 2500


def test_ceiling_env_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("MAX_TOKENS_PER_TURN_CEILING", "3000")
    monkeypatch.setenv("MAX_TOKENS_PER_TURN", "2500")
    s = Settings(_env_file=None)
    assert s.max_tokens_per_turn_ceiling == 3000
