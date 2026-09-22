/**
 * Module entry point — canonical `defineModule()` pattern (Module Federation 2.0).
 *
 * @nekazari/module-builder's `nkzModulePreset()` exposes this file as `./Module`
 * and builds it into `dist/remoteEntry.js`. The host loads it at runtime via
 * `registerRemotes` + `loadRemote('agent/Module')`.
 *
 * Do NOT call `window.__NKZ__.register()` here — that IIFE pattern no longer
 * works under Module Federation 2.0. Export the `defineModule()` result instead;
 * the builder + host runtime derive registration, slots and manifest from it.
 *
 * agent must match the `id` column in marketplace_modules exactly.
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
