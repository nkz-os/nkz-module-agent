/**
 * Module entry point — canonical `defineModule()` pattern (Module Federation 2.0).
 *
 * `@nekazari/module-builder`'s `nkzModulePreset()` detects this file (the
 * "modern" entry strategy — see `detectEntryStrategy` in module-builder),
 * exposes it as `./Module`, and — critically — only in this strategy also
 * emits `dist/manifest.json` (the NKZ data manifest api-gateway reads for
 * CSP-of-data enforcement, and that entity-manager's publish endpoint
 * requires to be present at all). The legacy `src/moduleEntry.ts` form this
 * module used to ship never got that manifest emitted. `nkz-module-soil` and
 * `nkz-module-cue` already use this same modern form.
 *
 * The host loads it at runtime via `registerRemotes` + `loadRemote('agent/Module')`.
 * agent must match the `id` column in marketplace_modules exactly, and the
 * `id` below, and the repo's root `manifest.json#id`.
 */
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
  navigation: {
    label: { es: 'Asistente por chat', en: 'Chat assistant' },
    section: 'modules',
    // ASSUMPTION: no ordering spec was provided for this module; picked a
    // mid-range value consistent with other modules' priority (e.g. soil=40).
    priority: 50,
  },
  slots: moduleSlots as never,
});
