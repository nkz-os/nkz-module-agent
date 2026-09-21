# Setup guide

> For full documentation see [README.md](README.md).

## 1. Clone and rename

```bash
git clone https://github.com/nkz-os/nkz-module-template.git my-module
cd my-module
```

## 2. Replace placeholders

Find-and-replace across all files (this includes `src/locales/*.json` and
`src/moduleEntry.ts` — the substitution walks every file, not just config):

| Placeholder | Replace with | Example |
|-------------|--------------|---------|
| `agent` | Your module ID (lowercase, hyphens) | `soil-sensor` |
| `Agent` | Human-readable name | `Soil Sensor` |
| `agent` | URL path | `/soil-sensor` |
| `nkz-os` | GitHub org | `acme-corp` |
| `nkz-os` | Author name | `Jane Smith` |

Or run the interactive initializer: `bash scripts/init-module.sh`.

## 3. Install dependencies

```bash
pnpm install
```

## 4. Configure environment

```bash
cp env.example .env
# Edit .env — set VITE_PROXY_TARGET and VITE_API_URL to your API domain
```

## 5. Develop

```bash
pnpm run dev
# http://localhost:5003 — dev shell only, not the production slot
```

## 6. Build

```bash
pnpm run build:module
# → dist/remoteEntry.js, dist/mf-manifest.json, dist/assets/*
#   (Module Federation 2.0 remote — see vite.config.ts / @nekazari/module-builder)
```

Fill in your real i18n strings in `src/locales/en.json` / `es.json` before
shipping — `ca.json` / `eu.json` / `fr.json` / `pt.json` ship as empty `{}`
skeletons (i18next falls back to `en` for missing keys).

## 7. Upload to MinIO

```bash
# On the server with port-forward active — upload the whole dist/ directory,
# not a single file. The host's federation runtime fetches remoteEntry.js and
# chunks relative to mf-manifest.json's publicPath.
mc cp --recursive dist/ minio/nekazari-frontend/modules/agent/
```

## 8. Register in database (required)

```bash
psql -U postgres -d nekazari -f k8s/registration.sql
```

Your `marketplace_modules.metadata` must include routing keys used by api-gateway auto-proxy:

- `api_prefix` (example: `/api/agent`)
- `backend_service` (example: `http://agent-api-service:8000`)
- `backend_mount` (example: `/api/agent`)
- `requires_auth` (`true` unless the endpoint is intentionally public)

Quick verification:

```sql
SELECT id, metadata->>'api_prefix', metadata->>'backend_service'
FROM marketplace_modules
WHERE id = 'agent';
```

After the first CI publish, entity-manager invalidates the api-gateway route cache automatically (`POST /internal/cache/invalidate` with `key=routes`). No manual cache flush is required.

## 9. Deploy backend (if any)

```bash
docker build -t ghcr.io/nkz-os/agent-backend:v1.0.0 ./backend
docker push ghcr.io/nkz-os/agent-backend:v1.0.0
kubectl apply -f k8s/backend-deployment.yaml -n nekazari
```

Do not add a dedicated `/api/agent` Ingress rule. Module API traffic should go through the platform `/api` catch-all and gateway auto-proxy, except for explicitly approved direct-ingress exceptions.

This backend trusts api-gateway-injected headers (`X-Tenant-ID`, `X-User-ID`,
`X-User-Roles`) — it never talks to Keycloak directly. Set
`INTERNAL_SERVICE_SECRET` (K8s Secret `internal-service-secret`, org-level)
for the `/internal/*` routes called by entity-manager and other in-cluster
services; without it those routes always reject with 401.

## 10. Activate for tenants

Tenants enable the module via the platform UI, or directly:

```sql
INSERT INTO tenant_installed_modules (tenant_id, module_id, is_active)
VALUES ('your-tenant', 'agent', true)
ON CONFLICT DO NOTHING;
```
