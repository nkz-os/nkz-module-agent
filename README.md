# nkz-module-agent

Conversational assistant reachable from a messaging channel (Telegram today) —
a Nekazari platform module.

**Version 1 is read-only.** It links a chat account to a platform tenant and
replies with a fixed placeholder once linked; it does not write to the
platform's context broker, record field operations, or command machinery. A
language model is not wired in yet on purpose. See
[`docs/index.md`](docs/index.md) for the full design rationale: the two
authentication surfaces (gateway-trusted management routes vs. a
shared-secret public webhook), the tenant-isolation model, the account
linking flow end to end, known limitations, and the test conventions that
must not be relaxed. That page is also what publishes to the public
documentation portal — read it before assuming this README is the whole
story.

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
nkz-module-agent/
├── src/
│   ├── Module.tsx               # export default defineModule({...}) — MF2 entry
│   ├── App.tsx                  # Main page: SlotShell + LinkPanel
│   ├── components/LinkPanel.tsx # Generate/list/revoke channel links
│   ├── main.tsx                 # Dev-only entry (Vite) — not part of the production bundle
│   ├── i18n.ts                  # i18next resource bundle registration
│   ├── locales/                 # en/es filled in; ca/eu/fr/pt ship as {} skeletons
│   └── slots/index.ts           # Declares which host slots this module occupies
├── backend/
│   └── app/
│       ├── api/__init__.py      # Management routes — gateway-trusted, require_auth()
│       ├── api/webhook.py       # Public channel webhook — shared-secret auth
│       ├── api/internal.py      # /internal/* — X-Internal-Service-Secret only
│       ├── channels/            # ChannelAdapter protocol + telegram.py
│       ├── domain/              # SessionContext, InboundMessage/OutboundMessage
│       ├── identity/            # Link-token and channel-link rules + SQL
│       ├── dedupe/               # Inbound update idempotency claim
│       ├── handlers.py          # One conversational turn
│       └── middleware/          # Gateway-header auth (nkz_platform_sdk.auth) +
│                                 # verify_internal_secret — NO JWKS/JWT here.
├── k8s/
│   ├── backend-deployment.yaml  # K8s Deployment + Service for backend
│   └── registration.sql         # Insert/update marketplace_modules
├── docs/index.md                 # Design rationale — publishes to the docs portal
├── manifest.json                 # NKZ metadata (routing, slots, data CSP) — edit by hand,
│                                  # read at registration/publish time, NOT emitted into dist/
├── vite.config.ts               # Uses @nekazari/module-builder preset (MF2)
├── package.json
└── dist/                        # `pnpm run build:module` output
    ├── remoteEntry.js           # Federation remote entry
    ├── mf-manifest.json         # Federation manifest (shared deps + exposes)
    ├── manifest.json            # Data manifest emitted from defineModule() — see below
    └── assets/                  # Sync + async chunks
```

---

## `defineModule()` — the single source of truth

The entry point is `src/Module.tsx` (not `moduleEntry.ts` — this module
migrated to the "modern" entry strategy so the builder also emits
`dist/manifest.json`; see the file's own header comment for why):

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
  route: '/module/agent',
  navigation: { label: { es: 'Asistente por chat', en: 'Chat assistant' }, section: 'modules', priority: 50 },
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

`src/services/api.ts` wraps `NKZClient` (also from `@nekazari/sdk`) with the module's `VITE_API_URL` base as a template pattern for calling this module's own backend — it is currently unused scaffolding; `LinkPanel.tsx` calls the backend with plain `fetch(..., { credentials: 'include' })` instead. There is **no `useConfig()` hook**; read the API base at build time via `import.meta.env.VITE_API_URL`.

You never write raw `fetch`, never handle JWT cookies, never construct `Fiware-Service` headers by hand.

---

## Build

```bash
pnpm run build:module
# → dist/remoteEntry.js, dist/mf-manifest.json, dist/manifest.json, dist/assets/*
#   (Module Federation 2.0 remote — upload the whole dist/ directory to MinIO)
```

The `@nekazari/module-builder@^2.0.3` preset (`nkzModulePreset()`) configures Module Federation 2.0 via `@module-federation/vite`:
- **Singleton shared deps** — `react`, `react-dom`, `@nekazari/*`, `i18next`, `react-i18next` resolved by the host at runtime. Never bundle them.
- **`src/Module.tsx`** → `export default defineModule({...})` is the single entry point exposed as `./Module`. The build emits `dist/remoteEntry.js` + `dist/mf-manifest.json` + `dist/assets/*`, and — because this module uses the modern entry strategy — also `dist/manifest.json`, a data manifest generated from the `defineModule()` call (currently declares no `data.entities`/`data.timeseries`, correct for a v1 that reads no platform entities; see `docs/index.md`'s forward note). That generated file is distinct from the root-level `manifest.json`, which is separate, hand-edited marketplace metadata (routing, slots, pricing) consumed at registration/publish time.

---

## Local development

```bash
pnpm run dev
# http://localhost:5003 — dev shell only, not the production slot
```

For integration with a real backend, set `VITE_PROXY_TARGET=https://your-api-domain` in `.env`.

---

## Backend

FastAPI app under `backend/app/`. Copy `env.example` to `.env` for local
runs; see that file for the meaning of every variable (deliberately no real
values — a default that names one deployment silently breaks every other
install of this module).

Two authentication surfaces exist and must stay separate — see
[`docs/index.md`](docs/index.md) for the full rationale:

- **Management routes** (`/link-tokens`, `/links`) trust `X-Tenant-ID` /
  `X-User-ID` / `X-User-Roles` headers injected by the platform's
  api-gateway, via `require_auth()`. They never validate a token themselves.
- **The channel webhook** (`/webhook/telegram`) is not behind the gateway —
  it authenticates with a shared secret (`TELEGRAM_WEBHOOK_SECRET`) compared
  with `hmac.compare_digest`, and acknowledges immediately while processing
  the update in the background.
- **`/internal/*`** routes authenticate with `X-Internal-Service-Secret`
  (`INTERNAL_SERVICE_SECRET`), for in-cluster callers only.

### Tests

The database-backed tests need a real PostgreSQL — the behaviour under test
(partial unique indexes, atomic single-use token updates, `ON CONFLICT`
races) does not exist in a mock:

```bash
docker run -d --name agent-test-db -p 55432:5432 \
    -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test postgres:15
export POSTGRES_URL=postgresql://postgres:test@localhost:55432/test

cd backend
pip install -r requirements.txt
python -m pytest tests/ -v
```

Tests that need the database are marked and skipped automatically when
`POSTGRES_URL` is unset. Read `backend/tests/conftest.py` before touching
any fixture in that file — several of them (in particular the autouse
connection-pool reset) exist for reasons that are not obvious from the code
alone; the docstrings explain why. The same applies to the security tests in
`backend/tests/test_webhook_auth.py` and the token test in
`backend/tests/test_identity_service.py` — see "Test conventions that must
not be relaxed" in `docs/index.md` before deleting anything there that looks
redundant.

---

## Deploy

Push to `main`. That's it.

The included `.github/workflows/build-push.yml` handles everything via GitHub Actions:

1. **Tests** — frontend typecheck + backend tests
2. **Build** — `pnpm run build:module` produces `dist/`, gated by a `guard` job that derives readiness from `BACKEND_IMAGE` itself (fails closed if the module id/org is still a placeholder) rather than a hand-set `if: false` — already green for this module's real id (`agent`)
3. **Publish** — uploads to immutable `modules/agent/<git-sha>/` on MinIO, flips the live pointer

The publish step uses **GitHub OIDC** for authentication:
- Runner gets a signed JWT from `token.actions.githubusercontent.com`
- `POST https://your-api-domain/api/internal/modules/agent/publish`
- No manual MinIO uploads, no manual cluster commands, no database SQL.

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
