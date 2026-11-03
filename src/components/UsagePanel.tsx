/**
 * UsagePanel — this month's assistant usage for the organisation.
 * Plain elements and host classes, like LinkPanel.
 */
import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getUsage, type Usage } from '../services/llmConfigApi';

export function UsagePanel() {
  const { t } = useTranslation('agent');
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    getUsage().then(setUsage).catch(() => setError(true));
  }, []);

  const cost = (v: number | null) => (v === null ? '—' : v.toFixed(4));

  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-nkz-text-primary font-medium">{t('usage.title')}</h3>
      {error && <p className="text-nkz-danger">{t('error')}</p>}
      {usage && usage.days.length === 0 && (
        <p className="text-nkz-text-muted">{t('usage.empty')}</p>
      )}
      {usage && usage.days.length > 0 && (
        <ul className="flex flex-col gap-2 text-nkz-sm text-nkz-text-primary">
            <li className="flex items-center justify-between gap-4 text-nkz-text-muted">
              <span>{t('usage.date')}</span>
              <span>{t('usage.turns')}</span>
              <span>{t('usage.tokensIn')}</span>
              <span>{t('usage.tokensOut')}</span>
              <span>{t('usage.cost')}</span>
            </li>
          {usage.days.map((d) => (
            <li key={d.date} className="flex items-center justify-between gap-4">
              <span>{d.date}</span>
              <span>{d.turns}</span>
              <span>{d.tokens_prompt}</span>
              <span>{d.tokens_completion}</span>
              <span>{cost(d.cost_estimate_usd)}</span>
            </li>
          ))}
            <li className="flex items-center justify-between gap-4 font-medium">
              <span>{t('usage.total')}</span>
              <span>{usage.totals.turns}</span>
              <span>{usage.totals.tokens_prompt}</span>
              <span>{usage.totals.tokens_completion}</span>
              <span>{cost(usage.totals.cost_estimate_usd)}</span>
            </li>
        </ul>
      )}
    </div>
  );
}
