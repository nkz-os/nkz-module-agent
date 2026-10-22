/**
 * i18n bundle registration — canonical pattern.
 *
 * MUST import `{ i18n }` from '@nekazari/sdk' as an ES module. Do NOT read
 * `window.__NKZ_SDK__` — it is injected by the host AFTER this module's code
 * has already loaded, so a window-global read here would silently no-op.
 * The ES module import guarantees the SDK singleton is available.
 */
import { i18n } from '@nekazari/sdk';
import en from './locales/en.json';
import es from './locales/es.json';

// Namespace must match the module id (agent) so t('key', { ns: 'agent' })
// and useTranslation('agent') resolve these bundles.
const NAMESPACE = 'agent';

// es + en are the platform minimum (see nkz AGENTS.md §3 Frontend — I18n).
// ca/eu/fr/pt are not registered: this module ships no translated content
// for them, and an empty bundle is not a translation — do not invent one.
// Add a locale here only alongside real content in src/locales/<code>.json.
export function registerModuleTranslations(): void {
  if (!i18n || typeof (i18n as any).addResourceBundle !== 'function') return;
  i18n.addResourceBundle('en', NAMESPACE, en, true, true);
  i18n.addResourceBundle('es', NAMESPACE, es, true, true);
}

registerModuleTranslations();
