-- =============================================================================
-- 001 — Conversational agent: dedicated module schema + adopt existing tables
-- =============================================================================
-- Platform contract (DB-roles migration, sprint 2): the login role
-- ``nkz_module_agent`` is created and password-managed BY THE PLATFORM, never
-- by this repo. Its password is sealed in the deployment environment. The
-- module owns this schema: the three identity/link tables (channel links,
-- link tokens, inbound-update dedupe) move out of the shared ``public``
-- schema into ``agent_module``, owned by that platform role. Admin/metadata
-- only; no time-series data here.
--
-- Because the schema is owned by the module's own role, every object below
-- is owned by it too — so future module migrations need no privileged DSN,
-- only this schema to exist. Creating the schema itself is the single
-- privileged bootstrap step (CREATE SCHEMA ... AUTHORIZATION).
--
-- The MOVE block runs BEFORE the CREATE TABLE statements on purpose. In a
-- database that already has these tables in ``public`` (production), creating
-- the qualified ``agent_module.*`` tables first would build empty duplicates
-- and then the SET SCHEMA below would collide. Moving first makes the CREATEs
-- no-ops there; on a virgin database the move finds nothing and the CREATEs
-- build the tables directly in ``agent_module``. Re-running is safe either way.
-- =============================================================================

-- Fail fast with a clear message instead of a cryptic GRANT/OWNER error when
-- the platform role is missing (fresh install run before the platform's
-- DB-roles migration).
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nkz_module_agent') THEN
    RAISE EXCEPTION
      'role nkz_module_agent not found: apply the platform DB-roles migration '
      '(it creates the nkz_module_* login roles) before running module migrations';
  END IF;
END $$;

-- Wrapped in a DO block on purpose: a bare ``CREATE SCHEMA IF NOT EXISTS``
-- still checks CREATE-on-database even when the schema already exists, so the
-- module's own role (which must NOT have that privilege) could never re-run
-- this file after the bootstrap. Checking pg_namespace first makes the
-- statement genuinely conditional: the privileged bootstrap creates the
-- schema owned by the module role, and every later run skips it.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'agent_module') THEN
    EXECUTE 'CREATE SCHEMA agent_module AUTHORIZATION nkz_module_agent';
  END IF;
END $$;

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

-- Converge ownership no matter who ran this file (a privileged bootstrap on a
-- virgin database would otherwise leave postgres-owned objects, outside the
-- platform model: the owner skips ENABLE row-level security but not FORCE, so
-- ownership IS the isolation boundary for this module's role). Index
-- ownership follows its table automatically, and so does the bigserial
-- sequence: a sequence linked to its table by OWNED BY cannot have its owner
-- altered separately (PostgreSQL rejects it), it moves with the table.
ALTER TABLE agent_module.agent_channel_links OWNER TO nkz_module_agent;
ALTER TABLE agent_module.agent_link_tokens OWNER TO nkz_module_agent;
ALTER TABLE agent_module.agent_processed_updates OWNER TO nkz_module_agent;

-- No table-level GRANTs: the module's role owns the objects, and ownership
-- implies every privilege. Grants to the owner would be dead weight that
-- tests could not turn red.
