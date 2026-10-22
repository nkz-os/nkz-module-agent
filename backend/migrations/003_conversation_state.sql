-- =============================================================================
-- 003 — Conversational agent: per-account 24h conversation memory
-- =============================================================================
-- One row per (tenant_id, channel, channel_user_id): the exact session
-- identity the rest of the turn already resolves. The tenant is part of the
-- primary key and of every query in app/agent/memory.py — isolation here is
-- the primary key plus the discipline that no statement forgets to qualify
-- all three columns, not row-level security (this table deliberately has
-- none). The audit table's FORCE RLS exists because audit rows are shared
-- multi-tenant history; conversation state is private by construction and is
-- never read except through its full key.
--
-- The row is re-written in place on every successful turn (ON CONFLICT ... DO
-- UPDATE), so there is no append-only concern and no per-turn row growth:
-- at most one row per account, ever.
-- =============================================================================

CREATE TABLE IF NOT EXISTS agent_module.agent_conversation_state (
    tenant_id        TEXT        NOT NULL,
    channel          TEXT        NOT NULL,
    channel_user_id  TEXT        NOT NULL,
    messages         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, channel, channel_user_id)
);

ALTER TABLE agent_module.agent_conversation_state OWNER TO nkz_module_agent;
