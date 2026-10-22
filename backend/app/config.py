"""
Agent Backend - Configuration

Environment-based configuration using pydantic-settings.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    app_name: str = "Agent"
    app_version: str = "1.0.0"
    debug: bool = False
    log_level: str = "INFO"  # Level for this module's own loggers; see app.main.configure_logging

    # API
    api_prefix: str = "/api/agent"
    cors_origins: list[str] = []  # Set via CORS_ORIGINS env var; empty = deny all cross-origin

    # Internal service-to-service auth (entity-manager, other in-cluster
    # callers -> this module's /internal/* routes). Compared with
    # hmac.compare_digest — NOT the api-gateway X-Auth-Signature HMAC format,
    # that one is user-bound (gateway -> backend proxy hop only). K8s Secret
    # `internal-service-secret` (org-level) in production. No default.
    internal_service_secret: str = ""

    # Database (required). The identity/link chain and update dedupe write
    # admin/metadata here (tenants, credentials, non-timeseries state) —
    # NEVER for time-series/telemetry, which must flow through Orion-LD
    # subscriptions. No hardcoded fallback: see require_postgres_url() below,
    # called from app.main's lifespan at startup.
    postgres_url: str = ""

    # Messaging channel (Telegram). All empty by default: a default that names
    # our deployment silently breaks everyone else's install.
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_bot_username: str = ""

    # Timeseries reader (core service). Module -> core auth uses the canonical
    # platform HMAC format (X-Auth-Signature = generate_hmac_signature(
    # HMAC_SECRET, "", tenant)) -- this is the SAME format as the api-gateway
    # user-bound signature, not the internal-service-secret format above.
    # Empty by default: no hardcoded URL or secret.
    timeseries_url: str = ""
    hmac_secret: str = ""

    # Module-backed read tools. Each URL points at an installed module; empty
    # by default so a missing module never points at someone else's deployment.
    weather_map_url: str = ""
    bioorch_api_url: str = ""
    cue_api_url: str = ""

    # Account linking
    link_token_ttl_seconds: int = 600

    # Inbound update dedupe retention
    dedupe_ttl_hours: int = 24

    # Conversational memory retention: a thread older than this is treated as
    # empty on load and reclaimed by the opportunistic purge on save.
    memory_ttl_hours: int = 24

    # Audit text retention (owner decision, option A): after this many days the
    # audit keeps metadata (outcome, tokens, latency, who/when), not content.
    audit_text_retention_days: int = 90

    # Language model. Configurable per deployment: every company installing this
    # platform uses its own. All empty by default — a default naming our model or
    # endpoint would break their install and leak ours.
    llm_model: str = ""          # e.g. "azure/<deployment>", "ollama/<model>"
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_temperature: float = 0.1

    # Hard per-turn budget. Without these an agent can loop, and a loop on a
    # public endpoint is unbounded spend and unbounded latency.
    max_iterations: int = 4
    max_tool_calls: int = 8
    max_tokens_per_turn: int = 4000
    turn_timeout_seconds: int = 45

    # Tool registry: how long one tenant's installed-module intersection is
    # cached in memory before the next turn re-reads public.module_installations.
    registry_cache_seconds: int = 60

    # Spend caps. The platform rule is fail-open for feature quotas; this is
    # money on a publicly reachable route, so the cap is finite by default.
    # Owner ruling 2026-10-18: start conservative (worst case ~60k tokens/h per
    # account with 15 turns x 4k); raise with real usage data, not in advance.
    max_turns_per_tenant_day: int = 500
    max_turns_per_account_hour: int = 15

    # Redis (for caching/celery - optional)
    # redis_url: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


def require_postgres_url() -> str:
    """Return POSTGRES_URL or fail fast.

    Call this from any code path that opens a PostgreSQL connection — do NOT
    hardcode a fallback DSN (platform rule: "POSTGRES_URL is MANDATORY —
    services must fail at startup if not set"). Called from app.main's
    lifespan at process startup (so a misconfigured deployment dies loudly
    instead of accepting traffic) and again from app.db.get_pool() as
    defense in depth.
    """
    settings = get_settings()
    if not settings.postgres_url:
        raise RuntimeError(
            "POSTGRES_URL is not set. Set the POSTGRES_URL env var "
            "(K8s Secret in production) before using PostgreSQL."
        )
    return settings.postgres_url
