-- =============================================================================
-- 101 — Conversational agent: channel links, link tokens, update dedupe
-- Idempotent. Admin/metadata only; no time-series data here.
-- =============================================================================

CREATE TABLE IF NOT EXISTS agent_channel_links (
    id              BIGSERIAL PRIMARY KEY,
    channel         TEXT        NOT NULL,
    channel_user_id TEXT        NOT NULL,
    tenant_id       TEXT        NOT NULL,
    user_id         TEXT        NOT NULL,
    roles           TEXT[]      NOT NULL DEFAULT '{}',
    status          TEXT        NOT NULL DEFAULT 'active',
    linked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ,
    last_seen_at    TIMESTAMPTZ,
    CONSTRAINT agent_channel_links_status_ck
        CHECK (status IN ('active', 'revoked'))
);

-- One ACTIVE link per channel account. This is what prevents a re-linked
-- account from ever resolving to two tenants.
CREATE UNIQUE INDEX IF NOT EXISTS agent_channel_links_active_uq
    ON agent_channel_links (channel, channel_user_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS agent_channel_links_tenant_idx
    ON agent_channel_links (tenant_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS agent_link_tokens (
    token_hash  TEXT        PRIMARY KEY,
    tenant_id   TEXT        NOT NULL,
    user_id     TEXT        NOT NULL,
    roles       TEXT[]      NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agent_link_tokens_expiry_idx
    ON agent_link_tokens (expires_at);

-- Inbound update dedupe. Messaging platforms retry on non-2xx; without this
-- a retry is a second execution of the whole turn.
CREATE TABLE IF NOT EXISTS agent_processed_updates (
    idempotency_key TEXT        PRIMARY KEY,
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_processed_updates_seen_idx
    ON agent_processed_updates (seen_at);
