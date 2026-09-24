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

-- Tenant isolation, enforced by the database rather than by every caller
-- remembering a WHERE clause. The policy reads the tenant from a session
-- setting, the same mechanism the platform's other audit tables use.
--
-- FORCE matters here: without it the table owner is exempt, and the owner is
-- the role that runs migrations. With it, only a role that is neither owner
-- nor superuser is actually constrained -- a superuser bypasses row security
-- unconditionally, FORCE included. So this policy is correct but INERT for a
-- client connecting as a superuser; it starts protecting the moment the
-- service connects with a least-privilege role of its own.
ALTER TABLE agent_turn_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_turn_audit FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_turn_audit_tenant_isolation ON agent_turn_audit;
CREATE POLICY agent_turn_audit_tenant_isolation ON agent_turn_audit
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
