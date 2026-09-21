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
import ca from './locales/ca.json';
import eu from './locales/eu.json';
import fr from './locales/fr.json';
import pt from './locales/pt.json';

// Namespace must match the module id (agent) so t('key', { ns: 'agent' })
// and useTranslation('agent') resolve these bundles.
const NAMESPACE = 'agent';

export function registerModuleTranslations(): void {
  if (!i18n || typeof (i18n as any).addResourceBundle !== 'function') return;
  i18n.addResourceBundle('en', NAMESPACE, en, true, true);
  i18n.addResourceBundle('es', NAMESPACE, es, true, true);
  i18n.addResourceBundle('ca', NAMESPACE, ca, true, true);
  i18n.addResourceBundle('eu', NAMESPACE, eu, true, true);
  i18n.addResourceBundle('fr', NAMESPACE, fr, true, true);
  i18n.addResourceBundle('pt', NAMESPACE, pt, true, true);
}

registerModuleTranslations();
