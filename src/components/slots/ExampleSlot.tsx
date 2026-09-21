/**
 * ExampleSlot — replace with your actual slot component.
 *
 * Slot components render inside host-provided containers.
 * - Access viewer context via useViewer() and useAuth() from @nekazari/sdk
 * - Use useTranslation() from @nekazari/sdk — every user-facing string goes
 *   through t(), never a hardcoded literal (see src/locales/*.json).
 * - Wrap the widget body in <SlotShell> from @nekazari/viewer-kit — it gives
 *   the panel chrome (title, accent scope, error boundary) that the viewer
 *   expects from every slot widget; do not hand-roll that shell.
 * - React, SDK, UI-Kit and Viewer-Kit are Module Federation shared singletons
 *   (see vite.config.ts) — do not bundle a second copy.
 * - Keep panels responsive (300–600px wide).
 */
import React, { useState } from 'react';
import { useViewer, useAuth, useTranslation } from '@nekazari/sdk';
import { SlotShell } from '@nekazari/viewer-kit';
import { AlertCircle, RefreshCw } from 'lucide-react';

// Must match the `accent` passed to defineModule() in moduleEntry.ts.
const moduleAccent = { base: '#3B82F6', soft: '#DBEAFE', strong: '#1D4ED8' };

interface ExampleSlotProps {
  className?: string;
}

export const ExampleSlot: React.FC<ExampleSlotProps> = ({ className }) => {
  const { t } = useTranslation('agent');
  const { selectedEntityId } = useViewer();
  const { isAuthenticated, user } = useAuth();
  const [loading, setLoading] = useState(false);

  if (!isAuthenticated) {
    return (
      <SlotShell moduleId="agent" title={t('example.title')} accent={moduleAccent} className={className}>
        <div className="flex items-center gap-2 text-amber-600">
          <AlertCircle className="w-4 h-4 flex-shrink-0" />
          <span className="text-sm">{t('example.authRequired')}</span>
        </div>
      </SlotShell>
    );
  }

  return (
    <SlotShell moduleId="agent" title={t('example.title')} accent={moduleAccent} className={className}>
      <div className="space-y-3">
        <div className="flex items-center justify-end">
          <button
            onClick={() => setLoading((l) => !l)}
            className="p-1 rounded hover:bg-slate-100 text-slate-500"
            aria-label={t('example.refresh')}
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>

        <div className="text-xs text-slate-500 space-y-1 bg-slate-50 rounded p-2">
          <div className="flex justify-between gap-2">
            <span>{t('example.entity')}:</span>
            <span className="font-mono text-slate-700 truncate">{selectedEntityId ?? '—'}</span>
          </div>
          <div className="flex justify-between gap-2">
            <span>{t('example.user')}:</span>
            <span className="text-slate-700 truncate">{user?.email ?? '—'}</span>
          </div>
        </div>

        <p className="text-xs text-slate-400 italic">{t('example.replaceHint')}</p>
      </div>
    </SlotShell>
  );
};

export default ExampleSlot;
