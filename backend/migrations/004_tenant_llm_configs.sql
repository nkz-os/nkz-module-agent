-- =============================================================================
-- 004 — Per-tenant LLM provider config (BYOK), tenant-scoped by RLS
-- =============================================================================
-- One row per tenant. The API key is stored only as Fernet ciphertext
-- (app/llm/keys.py); api_key_last4 exists so the panel can show "...a1b2"
-- without decrypting. Additive and idempotent: re-running changes nothing.
--
-- Same RLS convention as 002: the module's role OWNS the table, ENABLE skips
-- the owner but FORCE does not, so the policy constrains the service itself.
-- The policy reads the tenant from the app.current_tenant session setting.
-- =============================================================================

CREATE TABLE IF NOT EXISTS agent_module.tenant_llm_configs (
    tenant_id           TEXT        PRIMARY KEY,
    provider            TEXT        NOT NULL,
    model               TEXT        NOT NULL,
    api_base            TEXT,
    api_key_ciphertext  BYTEA       NOT NULL,
    api_key_last4       TEXT        NOT NULL,
    max_tokens_per_turn INTEGER,
    updated_by          TEXT        NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_llm_configs_max_tokens_ck
        CHECK (max_tokens_per_turn IS NULL OR max_tokens_per_turn > 0)
);

ALTER TABLE agent_module.tenant_llm_configs OWNER TO nkz_module_agent;

ALTER TABLE agent_module.tenant_llm_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_module.tenant_llm_configs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_llm_configs_tenant_isolation ON agent_module.tenant_llm_configs;
CREATE POLICY tenant_llm_configs_tenant_isolation ON agent_module.tenant_llm_configs
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

-- Which LLM served the turn: the tenant's own provider, the platform's, or
-- none (nothing configured / key unreadable). Old rows stay NULL.
ALTER TABLE agent_module.agent_turn_audit ADD COLUMN IF NOT EXISTS llm_origin TEXT;

ALTER TABLE agent_module.agent_turn_audit DROP CONSTRAINT IF EXISTS agent_turn_audit_llm_origin_ck;
ALTER TABLE agent_module.agent_turn_audit ADD CONSTRAINT agent_turn_audit_llm_origin_ck
    CHECK (llm_origin IS NULL OR llm_origin IN ('tenant', 'platform', 'none'));
