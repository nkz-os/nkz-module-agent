/**
 * Main page component for this module — lazily loaded as `MainPage` by
 * moduleEntry.ts's `defineModule({ main: MainPage })`. Part of the
 * production bundle (not a dev-only shell); also rendered standalone by
 * `npm run dev` via src/main.tsx.
 */
import React from 'react';
import { SlotShell } from '@nekazari/viewer-kit';
import { useTranslation } from 'react-i18next';
import { LinkPanel } from './components/LinkPanel';
import './i18n';
import './index.css';

// Must match the `accent` passed to defineModule() in moduleEntry.ts.
const moduleAccent = { base: '#3B82F6', soft: '#DBEAFE', strong: '#1D4ED8' };

export default function App() {
  const { t } = useTranslation('agent');
  return (
    <SlotShell moduleId="agent" accent={moduleAccent} title={t('title')}>
      <LinkPanel />
    </SlotShell>
  );
}
