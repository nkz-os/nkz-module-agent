-- =============================================================================
-- 001 — Conversational agent: dedicated module schema + adopt existing tables
-- =============================================================================
-- The module owns its schema: the three identity/link tables (channel links,
-- link tokens, inbound-update dedupe) move out of the shared ``public`` schema
-- into ``agent_module``, and the least-privilege ``agent_module`` role gets the
-- DML it needs. Admin/metadata only; no time-series data here.
--
-- The MOVE block runs BEFORE the CREATE TABLE statements on purpose. In a
-- database that already has these tables in ``public`` (production), creating
-- the qualified ``agent_module.*`` tables first would build empty duplicates
-- and then the SET SCHEMA below would collide. Moving first makes the CREATEs
-- no-ops there; on a virgin database the move finds nothing and the CREATEs
-- build the tables directly in ``agent_module``. Re-running is safe either way.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS agent_module;

GRANT USAGE ON SCHEMA agent_module TO agent_module;

DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables
           WHERE schemaname = 'public'
             AND tablename IN ('agent_channel_links','agent_link_tokens','agent_processed_updates')
  LOOP
    EXECUTE format('ALTER TABLE public.%I SET SCHEMA agent_module', t);
  END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS agent_module.agent_channel_links (
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
    ON agent_module.agent_channel_links (channel, channel_user_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS agent_channel_links_tenant_idx
    ON agent_module.agent_channel_links (tenant_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS agent_module.agent_link_tokens (
    token_hash  TEXT        PRIMARY KEY,
    tenant_id   TEXT        NOT NULL,
    user_id     TEXT        NOT NULL,
    roles       TEXT[]      NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agent_link_tokens_expiry_idx
    ON agent_module.agent_link_tokens (expires_at);

-- Inbound update dedupe. Messaging platforms retry on non-2xx; without this
-- a retry is a second execution of the whole turn.
CREATE TABLE IF NOT EXISTS agent_module.agent_processed_updates (
    idempotency_key TEXT        PRIMARY KEY,
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_processed_updates_seen_idx
    ON agent_module.agent_processed_updates (seen_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    agent_module.agent_channel_links,
    agent_module.agent_link_tokens,
    agent_module.agent_processed_updates
TO agent_module;

-- The sequence moves with its table via SET SCHEMA (production) or is created
-- in agent_module by the CREATE TABLE above (virgin). Either way the qualified
-- name resolves.
GRANT USAGE, SELECT ON SEQUENCE agent_module.agent_channel_links_id_seq TO agent_module;
