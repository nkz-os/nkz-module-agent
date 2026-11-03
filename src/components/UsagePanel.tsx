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
        <table className="text-nkz-sm text-nkz-text-primary">
          <thead>
            <tr className="text-nkz-text-muted text-left">
              <th className="pr-4">{t('usage.date')}</th>
              <th className="pr-4">{t('usage.turns')}</th>
              <th className="pr-4">{t('usage.tokensIn')}</th>
              <th className="pr-4">{t('usage.tokensOut')}</th>
              <th>{t('usage.cost')}</th>
            </tr>
          </thead>
          <tbody>
            {usage.days.map((d) => (
              <tr key={d.date}>
                <td className="pr-4">{d.date}</td>
                <td className="pr-4">{d.turns}</td>
                <td className="pr-4">{d.tokens_prompt}</td>
                <td className="pr-4">{d.tokens_completion}</td>
                <td>{cost(d.cost_estimate_usd)}</td>
              </tr>
            ))}
            <tr className="font-medium">
              <td className="pr-4">{t('usage.total')}</td>
              <td className="pr-4">{usage.totals.turns}</td>
              <td className="pr-4">{usage.totals.tokens_prompt}</td>
              <td className="pr-4">{usage.totals.tokens_completion}</td>
              <td>{cost(usage.totals.cost_estimate_usd)}</td>
            </tr>
          </tbody>
        </table>
      )}
    </div>
  );
}
