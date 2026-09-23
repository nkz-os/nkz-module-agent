-- =============================================================================
-- 102 — Conversational agent: per-turn audit trail
-- Idempotent. Admin/metadata only; no time-series data here.
--
-- One row per conversational turn. Written after the turn resolves, whether it
-- succeeded or failed — a turn that errored is exactly the one someone will ask
-- about later. Nothing in the module updates or deletes these rows: the value of
-- an audit trail is that it is append-only, and the absence of an update path is
-- the only thing enforcing that.
-- =============================================================================

CREATE TABLE IF NOT EXISTS agent_turn_audit (
    id                BIGSERIAL PRIMARY KEY,
    trace_id          TEXT        NOT NULL,
    tenant_id         TEXT        NOT NULL,
    user_id           TEXT        NOT NULL,
    channel           TEXT        NOT NULL,
    channel_user_id   TEXT        NOT NULL,
    inbound_text      TEXT,
    model             TEXT,
    tool_calls        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    reply_text        TEXT,
    tokens_prompt     INTEGER,
    tokens_completion INTEGER,
    latency_ms        INTEGER,
    outcome           TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT agent_turn_audit_outcome_ck
        CHECK (outcome IN ('ok', 'refused', 'budget_exhausted', 'unconfigured', 'error'))
);

CREATE INDEX IF NOT EXISTS agent_turn_audit_tenant_idx
    ON agent_turn_audit (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS agent_turn_audit_trace_idx
    ON agent_turn_audit (trace_id);
