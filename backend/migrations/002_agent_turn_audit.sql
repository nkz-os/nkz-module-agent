-- =============================================================================
-- 002 — Conversational agent: per-turn audit trail, tenant-scoped by RLS
-- =============================================================================
-- Born in the module schema (never public), unlike the identity tables that
-- are adopted from public by 001. Append-only by convention and by ownership
-- (INSERT/SELECT only in the service code — no UPDATE/DELETE path exists).
--
-- One row per conversational turn. Written after the turn resolves, whether it
-- succeeded or failed — a turn that errored is exactly the one someone will ask
-- about later. Nothing in the module updates or deletes these rows: the value
-- of an audit trail is that it is append-only, and the absence of an update
-- path is the only thing enforcing that.
-- =============================================================================

CREATE TABLE IF NOT EXISTS agent_module.agent_turn_audit (
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
    ON agent_module.agent_turn_audit (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS agent_turn_audit_trace_idx
    ON agent_module.agent_turn_audit (trace_id);

-- See 001: converge ownership to the module's platform role regardless of who
-- runs this file. The bigserial sequence is linked to the table (OWNED BY) and
-- follows its owner; altering it separately is rejected by PostgreSQL.
ALTER TABLE agent_module.agent_turn_audit OWNER TO nkz_module_agent;

-- Tenant isolation, enforced by the database rather than by every caller
-- remembering a WHERE clause. The policy reads the tenant from a session
-- setting, the same mechanism the platform's other audit tables use.
--
-- FORCE is the ratchet and it matters exactly because the module's own role
-- OWNS this table: an owner skips ENABLE policies but not FORCE, so with FORCE
-- the service role is constrained by the policy like anyone else. Only a
-- superuser bypasses row security unconditionally — and nothing connects as
-- one anymore.
ALTER TABLE agent_module.agent_turn_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_module.agent_turn_audit FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_turn_audit_tenant_isolation ON agent_module.agent_turn_audit;
CREATE POLICY agent_turn_audit_tenant_isolation ON agent_module.agent_turn_audit
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
