# nkz-module-agent

Conversational assistant over messaging channels — a Nekazari platform module, scaffolded from `nkz-module-template`.

Modules are built as **Module Federation 2.0 remotes** (`dist/remoteEntry.js` + `dist/mf-manifest.json` + `dist/assets/`) plus a `dist/manifest.json`. All are uploaded to MinIO and loaded at runtime by the host via `loadRemote()`. No build-time coupling to the host.

---

## Quick start

```bash
git clone https://github.com/nkz-os/nkz-module-agent.git
cd nkz-module-agent
pnpm install
```

---

## Structure

```
my-module/
├── src/
│   ├── moduleEntry.ts          # export default defineModule({...}) — MF2 entry
│   ├── App.tsx                 # Main page component (lazy-loaded via moduleEntry.ts)
│   ├── main.tsx                # Dev-only entry (Vite) — not part of the production bundle
│   ├── i18n.ts                 # i18next resource bundle registration
│   ├── locales/                # en/es filled in; ca/eu/fr/pt ship as {} skeletons
│   ├── slots/index.ts           # Declare which host slots you occupy
│   ├── components/slots/       # Slot React components (wrapped in <SlotShell>)
│   ├── services/api.ts         # API client template (VITE_API_URL base)
│   └── types/                  # TypeScript types
├── backend/                    # FastAPI backend (optional, delete if unused)
│   └── app/
│       ├── middleware/         # Gateway-header auth (nkz_platform_sdk.auth) —
│       │                       # NO JWKS/JWT validation in the module.
│       └── api/internal.py     # /internal/* — X-Internal-Service-Secret only
├── k8s/
│   ├── backend-deployment.yaml # K8s Deployment + Service for backend
│   └── registration.sql        # Insert/update marketplace_modules
├── manifest.json                # NKZ metadata (routing, slots, data CSP) — edit by hand,
│                                 # read at registration/publish time, NOT emitted into dist/
├── vite.config.ts              # Uses @nekazari/module-builder preset (MF2)
├── package.json
└── dist/                       # `pnpm run build:module` output
    ├── remoteEntry.js          # Federation remote entry
    ├── mf-manifest.json        # Federation manifest (shared deps + exposes)
    └── assets/                 # Sync + async chunks
```

---

## `defineModule()` — the single source of truth

Edit `src/moduleEntry.ts`:

```ts
import { defineModule } from '@nekazari/module-kit';
import { lazy } from 'react';
import './i18n';
import { moduleSlots } from './slots';
import pkg from '../package.json';

const MainPage = lazy(() => import('./App'));

export default defineModule({
  id: 'agent',
  displayName: 'Agent',
  version: pkg.version,
  hostApiVersion: '^2.0.0',
  description: 'Agent — Nekazari Platform Module',
  accent: { base: '#3B82F6', soft: '#DBEAFE', strong: '#1D4ED8' },
  icon: 'puzzle',
  main: MainPage,
  slots: moduleSlots as never,
});
```

Do **not** call `window.__NKZ__.register()` — that IIFE pattern no longer works under Module Federation 2.0. Export the `defineModule()` result instead; the builder and host runtime derive registration, slots and manifest from it.

`agent` must match the `id` column in `marketplace_modules` exactly.

---

## Hooks — talking to the platform

Everything comes from `@nekazari/sdk` (shared federation singleton, resolved by the host at runtime):

```tsx
import { useViewer, useAuth, useTranslation } from '@nekazari/sdk';
import { SlotShell } from '@nekazari/viewer-kit';

const { t } = useTranslation('agent');
const { selectedEntityId } = useViewer();
const { isAuthenticated, user, getToken, getTenantId } = useAuth();
```

For your own backend, `src/services/api.ts` wraps `NKZClient` (also from `@nekazari/sdk`) with the module's `VITE_API_URL` base — see that file for the pattern. There is **no `useConfig()` hook**; read the API base at build time via `import.meta.env.VITE_API_URL`.

You never write raw `fetch`, never handle JWT cookies, never construct `Fiware-Service` headers by hand.

---

## Build

```bash
pnpm run build:module
# → dist/remoteEntry.js, dist/mf-manifest.json, dist/assets/*
#   (Module Federation 2.0 remote — upload the whole dist/ directory to MinIO)
```

The `@nekazari/module-builder@^2.0.3` preset (`nkzModulePreset()`) configures Module Federation 2.0 via `@module-federation/vite`:
- **Singleton shared deps** — `react`, `react-dom`, `@nekazari/*`, `i18next`, `react-i18next` resolved by the host at runtime. Never bundle them.
- **`src/moduleEntry.ts`** → `export default defineModule({...})` is the single entry point exposed as `./Module`. The build emits `dist/remoteEntry.js` + `dist/mf-manifest.json` + `dist/assets/*`. The root-level `manifest.json` is separate — it is hand-edited metadata (routing, slots, data CSP) consumed at registration/publish time, not emitted into `dist/`.

---

## Local development

```bash
pnpm run dev
# http://localhost:5003 — dev shell only, not the production slot
```

For integration with a real backend, set `VITE_PROXY_TARGET=https://your-api-domain` in `.env`.

---

## Deploy

Push to `main`. That's it.

The included `.github/workflows/build-push.yml` handles everything via GitHub Actions:

1. **Tests** — frontend typecheck + backend tests
2. **Build** — `pnpm run build:module` produces `dist/` (disabled by default in the raw template — the placeholder `agent` id fails the builder's kebab-case validator; remove the job's `if: false` once you've replaced placeholders)
3. **Publish** — uploads to immutable `modules/agent/<git-sha>/` on MinIO, flips the live pointer

The publish step uses **GitHub OIDC** for authentication:
- Runner gets a signed JWT from `token.actions.githubusercontent.com`
- `POST https://nkz.robotika.cloud/api/internal/modules/agent/publish`
- No manual MinIO uploads. No `kubectl`. No database SQL.

**Prerequisites (one-time, org-level — already done for nkz-os):**
- Org secret `INTERNAL_SERVICE_SECRET` configured in GitHub Actions secrets
- Module registered in `marketplace_modules` (one-time SQL `INSERT`, see `k8s/registration.sql`)
- Module metadata includes gateway routing keys:
  - `api_prefix` (for example `/api/agent`)
  - `backend_service` (for example `http://agent-api-service:8000`)
  - `backend_mount` (for example `/api/agent`)
  - `requires_auth` (`true` by default)

After first publish, verify metadata was preserved:

```sql
SELECT id, metadata->>'api_prefix', metadata->>'backend_service'
FROM marketplace_modules
WHERE id = 'agent';
```

If `api_prefix` is `NULL`, re-apply the routing metadata migration in `nkz` and invalidate the gateway `routes` cache.

---

## Slots

Edit `src/slots/index.ts` to register your components in host slots:

```ts
import type { ModuleViewerSlots } from '@nekazari/sdk';
import { ExampleSlot } from '../components/slots/ExampleSlot';

const MODULE_ID = 'agent';

export const moduleSlots: ModuleViewerSlots = {
  'map-layer': [],
  'layer-toggle': [],
  'context-panel': [
    { id: 'agent-context', moduleId: MODULE_ID, component: 'ExampleSlot', localComponent: ExampleSlot, priority: 10 },
  ],
  'bottom-panel': [],
  'entity-tree': [],
  'dashboard-widget': [],
};
```

Available slot types:

| Slot | Where it renders |
|------|-----------------|
| `context-panel` | Side panel when an entity is selected |
| `bottom-panel` | Tabbed panel at the bottom of the viewer |
| `map-layer` | Overlay or toolbar button on the 3D map |
| `layer-toggle` | Toggle entry in the layer panel |
| `entity-tree` | Context menu in the entity tree |
| `dashboard-widget` | Card in the tenant dashboard |

Wrap every slot component's body in `<SlotShell>` from `@nekazari/viewer-kit` — it gives the panel chrome (title, accent scope, error boundary) the viewer expects; do not hand-roll that shell. See `src/components/slots/ExampleSlot.tsx`.

---

## CSP-of-data (api-gateway enforcement)

When the bundle calls a platform API, the gateway validates the requested NGSI-LD `type=` / Timescale hypertable against the module's declared data manifest (`data.entities` / `data.timeseries`). Declare exactly what your module needs — this is the platform's lightweight defence-in-depth, no replacement for sandboxing.

---

## Build rules (critical)

- **Keep `i18next@^23.11.0` and `react-i18next@^14.1.0`** — must match the host's singleton versions to avoid federation runtime version mismatch warnings.
- **Never bundle shared deps** — React, ReactDOM, `@nekazari/*`, i18next, react-i18next. They come from the host as federation singletons. Bundling creates two instances and breaks hooks.
- **`main` wrapper pattern** — `defineModule({ main: lazy(() => import('./App')) })` gives you a Suspense boundary for free; keep context providers, if any, inside `App.tsx`.
- **i18n via ES import, not `window.__NKZ_SDK__`** — `import { i18n } from '@nekazari/sdk'` guarantees the SDK singleton is available at module-eval time; a `window.__NKZ_SDK__` read does not (the host injects it after the module's code has already loaded).

---

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).
