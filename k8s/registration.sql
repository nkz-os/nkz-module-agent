-- =============================================================================
-- Agent — Marketplace Registration
-- =============================================================================
-- Run once per environment to register this module in marketplace_modules.
-- Tenants then activate it via the UI (tenant_installed_modules).
--
-- Columns match the deployed marketplace_modules schema, which differs from
-- older module templates: there is no module_type / required_plan_type /
-- pricing_tier — plan gating is the integer required_plan_level (0 = free).
-- `scope` and `exposed_module` are what the host uses to resolve the remote,
-- and must match the module id and the mf-manifest expose path respectively.
-- =============================================================================

INSERT INTO marketplace_modules (
    id, name, display_name, description,
    scope, exposed_module, remote_entry_url,
    version, author, route_path, label,
    is_local, is_active, required_plan_level, required_roles, metadata
) VALUES (
    'agent', 'agent', 'Agent',
    'Conversational assistant over messaging channels',
    'agent', './Module', '/modules/agent/mf-manifest.json',
    '1.0.0', 'nkz-os', '/agent', 'Agent',
    false, true, 0,
    ARRAY['Farmer','TechnicalConsultant','TenantAdmin','PlatformAdmin'],
    '{"icon": "💬", "color": "#3B82F6", "description_i18n": {"es": "Asistente conversacional por canales de mensajería", "en": "Conversational assistant over messaging channels"}}'::jsonb
) ON CONFLICT (id) DO UPDATE SET
    display_name     = EXCLUDED.display_name,
    description      = EXCLUDED.description,
    scope            = EXCLUDED.scope,
    exposed_module   = EXCLUDED.exposed_module,
    remote_entry_url = EXCLUDED.remote_entry_url,
    required_roles   = EXCLUDED.required_roles,
    is_active        = true,
    updated_at       = NOW();
